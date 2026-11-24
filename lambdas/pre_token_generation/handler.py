"""Pre-token-generation Lambda trigger for Cognito.

Copies the administrator-assigned ``custom:tenant_id`` attribute into a
``tenant_id`` claim on the ID token so the frontend can show which tenant the
user belongs to.

Only administrators can set ``custom:tenant_id``. Every app client in the user
pool leaves it out of ``WriteAttributes``, so users can't change it with
UpdateUserAttributes. The agent doesn't rely on this claim: it looks up the
tenant from Cognito with the caller's access token on every request.
"""

TENANT_ATTRIBUTE = "custom:tenant_id"


def handler(event, context):
    """Cognito pre-token-generation trigger handler (event version V1_0).

    Refuses to issue tokens to users with no tenant assigned. Raising here
    makes Cognito fail the sign-in or token refresh, so a token without a
    tenant is never issued.
    """
    user_attributes = event.get("request", {}).get("userAttributes") or {}
    tenant_id = (user_attributes.get(TENANT_ATTRIBUTE) or "").strip()
    if not tenant_id:
        raise ValueError("User is not assigned to a tenant")

    event["response"] = {
        "claimsOverrideDetails": {
            "claimsToAddOrOverride": {
                "tenant_id": tenant_id,
            },
        },
    }
    return event
