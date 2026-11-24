"""Server-side tenant isolation for the SaaS Billing Agent.

Three controls live here:

1. ``resolve_tenant_id`` works out the caller's tenant from Cognito, using the
   caller's own access token. The AgentCore Runtime JWT authorizer has already
   checked that token (issuer, signature, expiry, FrontendClient client_id).
   Cognito ``GetUser`` checks it again, including revocation, and returns the
   user's current attributes. Only administrators can write
   ``custom:tenant_id``, because no app client in the stack lists it in
   ``WriteAttributes``.

   Client-controlled input is never used as the tenant: not custom headers,
   not fields in the JSON payload, and not prompt text.

2. ``sign_tenant_assertion`` produces the proof of that tenant that the service
   Lambdas check. The agent attaches it to every Gateway request. The Lambdas
   take the tenant from it and ignore any ``tenant_id`` in the event, so
   isolation doesn't depend on the agent alone. The tool schemas no longer
   declare a ``tenant_id`` argument.

3. ``TenantScopeHook`` is a second check on the model's tool calls. The model
   chooses tool arguments, so a prompt such as "show tenant-beta's invoices"
   can make it name another tenant. The hook:

   - rejects calls that name a different tenant, with a clear error
   - drops arguments the tool's input schema doesn't declare (this removes any
     ``tenant_id`` the model adds)
   - fills in the verified tenant if a tool schema declares ``tenant_id``
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Iterable, Mapping

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

TENANT_ATTRIBUTE = "custom:tenant_id"
TENANT_ARGUMENT = "tenant_id"

# Signed tenant assertion sent to the Gateway. lambdas/common/python/tenant_auth.py
# verifies it; keep the format, header name and algorithm in sync.
ASSERTION_HEADER = "x-tenant-assertion"
ASSERTION_VERSION = "v1"
MAC_ALGORITHM = "HMAC_SHA_256"
KEY_ID_ENV = "TENANT_ASSERTION_KEY_ID"
# The MCP session stays open for the whole agent turn, so one assertion must outlive
# a long turn. The verifier rejects anything valid for more than 600 seconds.
ASSERTION_LIFETIME_SECONDS = 540


class TenantResolutionError(Exception):
    """The caller's tenant could not be established from a verified identity."""


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------

_cognito_client = None
_kms_client = None


def _kms():
    global _kms_client
    if _kms_client is None:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        _kms_client = boto3.client("kms", region_name=region)
    return _kms_client


def _cognito():
    global _cognito_client
    if _cognito_client is None:
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        _cognito_client = boto3.client("cognito-idp", region_name=region)
    return _cognito_client


def _bearer_token(request_headers: Mapping[str, str] | None) -> str:
    headers = {str(k).lower(): v for k, v in (request_headers or {}).items()}
    scheme, _, token = (headers.get("authorization") or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise TenantResolutionError("missing bearer token")
    return token


def resolve_tenant_id(request_headers: Mapping[str, str] | None) -> str:
    """Return the administrator-assigned tenant of the user who owns the access token.

    Raises:
        TenantResolutionError: the token is missing or rejected by Cognito, or
            the user has no tenant assigned. Callers must fail closed.
    """
    token = _bearer_token(request_headers)
    try:
        user = _cognito().get_user(AccessToken=token)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        raise TenantResolutionError(f"Cognito rejected the access token ({code})") from exc
    except BotoCoreError as exc:
        raise TenantResolutionError(f"Cognito lookup failed ({type(exc).__name__})") from exc

    attributes = {a.get("Name"): a.get("Value") for a in user.get("UserAttributes", [])}
    tenant_id = (attributes.get(TENANT_ATTRIBUTE) or "").strip()
    if not tenant_id:
        raise TenantResolutionError("user has no tenant assigned")
    return tenant_id


# ---------------------------------------------------------------------------
# Signed tenant assertion for the service Lambdas
# ---------------------------------------------------------------------------

def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_tenant_assertion(tenant_id: str, now: float | None = None) -> str:
    """Return a short-lived KMS-signed assertion that the caller belongs to *tenant_id*.

    *tenant_id* must come from ``resolve_tenant_id``. The Gateway passes the
    assertion to the service Lambdas, which take the tenant from it instead of
    from tool arguments.

    Raises:
        TenantResolutionError: signing isn't configured or KMS refused. Callers
            must fail closed.
    """
    key_id = os.environ.get(KEY_ID_ENV, "")
    if not key_id:
        raise TenantResolutionError(f"{KEY_ID_ENV} is not configured")
    if not tenant_id:
        raise TenantResolutionError("no tenant to assert")

    issued_at = int(time.time() if now is None else now)
    payload = json.dumps({"tid": tenant_id, "iat": issued_at, "exp": issued_at + ASSERTION_LIFETIME_SECONDS},
                         separators=(",", ":"), sort_keys=True)
    signing_input = f"{ASSERTION_VERSION}.{_b64encode(payload.encode('utf-8'))}"
    try:
        mac = _kms().generate_mac(KeyId=key_id, Message=signing_input.encode("ascii"),
                                  MacAlgorithm=MAC_ALGORITHM)["Mac"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        raise TenantResolutionError(f"could not sign tenant assertion ({code})") from exc
    except BotoCoreError as exc:
        raise TenantResolutionError(f"could not sign tenant assertion ({type(exc).__name__})") from exc
    return f"{signing_input}.{_b64encode(mac)}"


# ---------------------------------------------------------------------------
# Tool-call binding
# ---------------------------------------------------------------------------

def _declared_arguments(tool: Any) -> set[str]:
    """Return the argument names declared in a tool's JSON input schema."""
    try:
        schema = tool.tool_spec["inputSchema"]
        schema = schema.get("json", schema)
        return set((schema.get("properties") or {}).keys())
    except (AttributeError, KeyError, TypeError):
        return set()


class TenantScopeHook(HookProvider):
    """Forces every Gateway tool call to use the caller's verified tenant."""

    def __init__(self, tenant_id: str, gateway_tool_names: Iterable[str]) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        self.tenant_id = tenant_id
        self.gateway_tool_names = frozenset(gateway_tool_names)

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.enforce)

    def enforce(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        tool_name = tool_use.get("name", "")
        if tool_name not in self.gateway_tool_names:
            return  # Local tools (Code Interpreter) don't read tenant data.

        tool_input = tool_use.get("input")
        if tool_input is None:
            tool_input = {}
        if not isinstance(tool_input, dict):
            event.cancel_tool = "Tool input must be a JSON object."
            return

        requested = tool_input.get(TENANT_ARGUMENT)
        if requested not in (None, "") and requested != self.tenant_id:
            logger.warning("Blocked cross-tenant tool call: tool=%s tenant=%s requested=%r",
                           tool_name, self.tenant_id, requested)
            event.cancel_tool = (
                f"Access denied: this session can only access data for tenant '{self.tenant_id}'."
            )
            return

        declared = _declared_arguments(event.selected_tool)
        scoped = {k: v for k, v in tool_input.items() if k in declared}
        dropped = sorted(set(tool_input) - set(scoped))
        if dropped:
            logger.warning("Dropped undeclared arguments: tool=%s args=%s", tool_name, dropped)
        if TENANT_ARGUMENT in declared:
            scoped[TENANT_ARGUMENT] = self.tenant_id

        event.tool_use = {**tool_use, "input": scoped}
