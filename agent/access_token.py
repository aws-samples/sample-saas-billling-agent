"""OAuth2 access token management for MCP Gateway authentication.

Two methods (matching the official device-management-agent sample):
1. AgentCore Identity @requires_access_token with M2M flow (preferred in Runtime)
2. Direct Cognito client_credentials flow (fallback for local dev)

The credential provider (SaaSBillingCredentialProvider) is configured with the
same Cognito M2M client that the Gateway's allowedClients expects.
"""

import os
import logging
import requests
import boto3

logger = logging.getLogger(__name__)

# ── Method 1: AgentCore Identity (preferred inside Runtime) ─────────

def _get_token_via_identity() -> str | None:
    """Get OAuth2 token via AgentCore Identity credential provider.

    Uses @requires_access_token with M2M flow. The credential provider
    exchanges workload identity for a Cognito access token via client_credentials.
    """
    try:
        from bedrock_agentcore.identity.auth import requires_access_token

        provider_name = os.environ.get("CREDENTIAL_PROVIDER_NAME", "SaaSBillingCredentialProvider")
        scope = os.environ.get("GATEWAY_SCOPE", "saas-billing/billing")

        @requires_access_token(
            provider_name=provider_name,
            scopes=[scope],
            auth_flow="M2M",
        )
        def _fetch(access_token: str = ""):
            return access_token

        token = _fetch()
        if token:
            logger.info("Got token via AgentCore Identity (provider=%s)", provider_name)
            return token
    except ValueError as e:
        if "Workload access token has not been set" in str(e):
            logger.info("Workload access token not available (not in Runtime), falling back")
        else:
            logger.warning("Identity auth ValueError: %s", e)
    except Exception as e:
        logger.info("AgentCore Identity not available: %s", e)

    return None


# ── Method 2: Direct Cognito client_credentials (fallback) ─────────

def _resolve_client_secret() -> str:
    """Resolve the M2M client secret from Secrets Manager or env var."""
    secret_arn = os.environ.get("GATEWAY_CLIENT_SECRET_ARN", "")
    if secret_arn:
        try:
            sm = boto3.client("secretsmanager")
            return sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        except Exception as e:
            logger.warning("Failed to read secret from Secrets Manager: %s", e)
    logger.warning("Client secret not available from Secrets Manager")
    return ""


def _get_token_via_cognito() -> str | None:
    """Get OAuth2 token directly from Cognito using client_credentials flow.

    Used as fallback when AgentCore Identity is not available (local dev,
    containerized environments without workload identity).
    """
    cognito_domain = os.environ.get("COGNITO_DOMAIN", "")
    client_id = os.environ.get("GATEWAY_CLIENT_ID", "")
    client_secret = _resolve_client_secret()
    scope = os.environ.get("GATEWAY_SCOPE", "saas-billing/billing")

    if not all([cognito_domain, client_id, client_secret]):
        logger.warning("Cognito OAuth config incomplete")
        return None

    try:
        token_url = f"{cognito_domain}/oauth2/token"
        resp = requests.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        logger.info("Got token via direct Cognito client_credentials")
        return token
    except Exception as e:
        logger.error("Failed to get Cognito token: %s", e)
        return None


# ── Main entry point ────────────────────────────────────────────────

def get_gateway_access_token() -> str:
    """Get access token for MCP Gateway.

    Tries AgentCore Identity first (correct flow for production Runtime),
    falls back to direct Cognito (for local dev / Docker without Identity).
    """
    # Method 1: AgentCore Identity (preferred)
    token = _get_token_via_identity()
    if token:
        return token

    # Method 2: Direct Cognito (fallback)
    logger.info("Falling back to direct Cognito authentication")
    token = _get_token_via_cognito()
    if token:
        return token

    raise RuntimeError(
        "Failed to get gateway access token via any method. "
        "Check CREDENTIAL_PROVIDER_NAME, COGNITO_DOMAIN, GATEWAY_CLIENT_ID, and GATEWAY_CLIENT_SECRET_ARN."
    )
