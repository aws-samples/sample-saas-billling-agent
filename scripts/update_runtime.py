"""Force Runtime to pull new container by updating to digest-based URI."""
import boto3

REGION = "us-east-1"
STACK = "SaaSBillingStack"
ECR_REPO = f"{STACK.lower()}-agent"

cfn = boto3.client("cloudformation", region_name=REGION)
ecr = boto3.client("ecr", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)

# Stack outputs
outputs = {o["OutputKey"]: o["OutputValue"] for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]}
RUNTIME_ID = outputs["RuntimeId"]

# Latest image digest
digest = ecr.describe_images(repositoryName=ECR_REPO, imageIds=[{"imageTag": "latest"}])["imageDetails"][0]["imageDigest"]
account_id = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
uri = f"{account_id}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}@{digest}"
print(f"New URI: {uri}")

# Keep every environment variable the Runtime already has and change only what this script
# owns. Rebuilding the whole set from scratch dropped values it couldn't look up.
current = ac.get_agent_runtime(agentRuntimeId=RUNTIME_ID)["environmentVariables"]

# M2M client secret: take it from the stack (its generated name has no stack prefix), and
# fall back to the value the Runtime already has. Never write an empty value.
secret_arn = ""
for res in cfn.describe_stack_resources(StackName=STACK)["StackResources"]:
    if res["ResourceType"] == "AWS::SecretsManager::Secret" and res["LogicalResourceId"].startswith("M2MClientSecret"):
        secret_arn = sm.describe_secret(SecretId=res["PhysicalResourceId"])["ARN"]
secret_arn = secret_arn or current.get("GATEWAY_CLIENT_SECRET_ARN", "")
if not secret_arn:
    raise SystemExit("Could not determine GATEWAY_CLIENT_SECRET_ARN; refusing to update the Runtime without it.")

pool_id = outputs["UserPoolId"]

resp = ac.update_agent_runtime(
    agentRuntimeId=RUNTIME_ID,
    agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
    roleArn=outputs["AgentRoleArn"],
    networkConfiguration={"networkMode": "PUBLIC"},
    authorizerConfiguration={
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
            "allowedClients": [outputs["FrontendClientId"]],
        }
    },
    requestHeaderConfiguration={
        # Keep in sync with infra/stacks/unified_stack.py. The agent derives the tenant from
        # the access token, so no tenant header is forwarded.
        "requestHeaderAllowlist": ["Authorization"]
    },
    environmentVariables={
        **current,
        "AGENTCORE_GATEWAY_URL": outputs["GatewayUrl"],
        "MEMORY_ID": outputs["MemoryId"],
        "CODE_INTERPRETER_ID": outputs["CodeInterpreterId"],
        "CREDENTIAL_PROVIDER_NAME": "SaaSBillingCredentialProvider",
        "TENANT_ASSERTION_KEY_ID": outputs["TenantAssertionKeyId"],
        "COGNITO_DOMAIN": outputs["CognitoDomain"],
        "GATEWAY_CLIENT_ID": outputs["M2MClientId"],
        "GATEWAY_CLIENT_SECRET_ARN": secret_arn,
        "GATEWAY_SCOPE": "saas-billing/billing",
        "AWS_DEFAULT_REGION": REGION,
        "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
    description="SaaS Billing Agent",
)
print(f"Status: {resp['status']}")
