"""Tenant identity for the service Lambdas (usage, billing, entitlement).

The Gateway calls these Lambdas with the agent's machine (M2M) identity, so
nothing in the call says which tenant the end user belongs to. A tool argument
can't carry that either: the model chooses arguments, and anyone who can call
the Gateway or a Lambda could put any tenant in them.

Instead the agent attaches a signed tenant assertion to every Gateway request.
The Gateway forwards it to the Lambda as the allowlisted ``x-tenant-assertion``
header, which appears in ``context.client_context.custom``. This module checks
the signature with KMS ``VerifyMac`` and returns the tenant it names.

The HMAC key can be used to sign only by the agent role (``kms:GenerateMac``)
and to verify only by these Lambdas (``kms:VerifyMac``). A compromised Lambda
can't mint an assertion for another tenant.

Assertion format: ``v1.<base64url(payload JSON)>.<base64url(HMAC-SHA-256)>``,
where the MAC covers the first two segments. The payload holds ``tid`` (tenant),
``iat`` and ``exp`` (Unix seconds).

Lambdas fail closed. Any problem raises ``TenantAuthError``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

ASSERTION_HEADER = "x-tenant-assertion"
ASSERTION_VERSION = "v1"
MAC_ALGORITHM = "HMAC_SHA_256"
KEY_ID_ENV = "TENANT_ASSERTION_KEY_ID"

MAX_LIFETIME_SECONDS = 600
CLOCK_SKEW_SECONDS = 30
_TOOL_NAME_DELIMITER = "___"

_kms_client = None


class TenantAuthError(Exception):
    """The caller's tenant could not be established from a valid assertion."""


def _kms():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _custom_context(context) -> dict:
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    return custom if isinstance(custom, dict) else {}


def _propagated_headers(context) -> dict:
    headers = _custom_context(context).get("bedrockAgentCorePropagatedHeaders")
    if isinstance(headers, str):  # propagatedHeadersEncoding=JSON_STRING
        try:
            headers = json.loads(headers)
        except ValueError:
            return {}
    if not isinstance(headers, dict):
        return {}
    return {str(name).lower(): value for name, value in headers.items()}


def gateway_tool_name(context) -> str | None:
    """Return the tool the Gateway is invoking, without the target-name prefix.

    The Gateway sets this in the Lambda context. Callers can't choose it, unlike
    the argument-based guessing the handlers fall back on without a Gateway.
    """
    name = _custom_context(context).get("bedrockAgentCoreToolName")
    if not isinstance(name, str) or not name:
        return None
    return name.split(_TOOL_NAME_DELIMITER, 1)[1] if _TOOL_NAME_DELIMITER in name else name


def verified_tenant_id(context, now: float | None = None) -> str:
    """Return the tenant named by a valid, unexpired assertion in *context*.

    Raises:
        TenantAuthError: the assertion is missing, malformed, expired, has a bad
            signature, or can't be verified.
    """
    assertion = _propagated_headers(context).get(ASSERTION_HEADER)
    if not isinstance(assertion, str) or not assertion:
        raise TenantAuthError("missing tenant assertion")

    parts = assertion.split(".")
    if len(parts) != 3 or parts[0] != ASSERTION_VERSION:
        raise TenantAuthError("malformed tenant assertion")

    key_id = os.environ.get(KEY_ID_ENV, "")
    if not key_id:
        raise TenantAuthError(f"{KEY_ID_ENV} is not configured")

    try:
        mac = _b64decode(parts[2])
        result = _kms().verify_mac(
            KeyId=key_id,
            Message=f"{parts[0]}.{parts[1]}".encode("ascii"),
            MacAlgorithm=MAC_ALGORITHM,
            Mac=mac,
        )
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise TenantAuthError("malformed tenant assertion") from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        if code == "KMSInvalidMacException":
            raise TenantAuthError("invalid tenant assertion signature") from exc
        raise TenantAuthError(f"tenant assertion could not be verified ({code})") from exc
    except BotoCoreError as exc:
        raise TenantAuthError(f"tenant assertion could not be verified ({type(exc).__name__})") from exc
    if not result.get("MacValid"):
        raise TenantAuthError("invalid tenant assertion signature")

    try:
        payload = json.loads(_b64decode(parts[1]))
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise TenantAuthError("malformed tenant assertion") from exc
    if not isinstance(payload, dict):
        raise TenantAuthError("malformed tenant assertion")

    tenant_id = payload.get("tid")
    issued_at, expires_at = payload.get("iat"), payload.get("exp")
    numbers = (int, float)
    if (not isinstance(tenant_id, str) or not tenant_id.strip()
            or isinstance(issued_at, bool) or not isinstance(issued_at, numbers)
            or isinstance(expires_at, bool) or not isinstance(expires_at, numbers)):
        raise TenantAuthError("malformed tenant assertion")

    current = time.time() if now is None else now
    if current >= expires_at:
        raise TenantAuthError("tenant assertion expired")
    if issued_at > current + CLOCK_SKEW_SECONDS:
        raise TenantAuthError("tenant assertion issued in the future")
    if expires_at - issued_at > MAX_LIFETIME_SECONDS:
        raise TenantAuthError("tenant assertion lifetime too long")

    return tenant_id.strip()
