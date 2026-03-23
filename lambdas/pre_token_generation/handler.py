"""Pre-token-generation Lambda trigger for Cognito.

Injects the custom `tenant_id` attribute into JWT claims so downstream
services (AgentCore Runtime, Gateway) can extract tenant identity from
the token without an extra Cognito lookup.
"""


def handler(event, context):
    """Cognito pre-token-generation trigger handler.

    Reads the custom:tenant_id user attribute and adds it as a claim
    override in the issued token.
    """
    user_attributes = event["request"].get("userAttributes", {})
    tenant_id = user_attributes.get("custom:tenant_id", "")

    event["response"] = {
        "claimsOverrideDetails": {
            "claimsToAddOrOverride": {
                "tenant_id": tenant_id,
            },
        },
    }
    return event
