"""UnifiedStack — Complete SaaS Billing Agent infrastructure.

Single `cdk deploy` provisions everything:
- DynamoDB tables, Cognito, Lambdas, IAM
- AgentCore: Memory, Gateway+Targets, Identity, CodeInterpreter, Runtime
- OAuth2 Credential Provider (Custom Resource)
- S3+CloudFront frontend
- ECR + CodeBuild for container image

Deploy: cdk deploy
Destroy: cdk destroy --force
"""

import os

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_codebuild as codebuild,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as secretsmanager,
    CustomResource,
    Duration,
    RemovalPolicy,
)
from constructs import Construct


# ── MCP tool schemas ────────────────────────────────────────────────

USAGE_TOOLS = [
    {"name": "get_usage_summary", "description": "Get total API usage for a tenant in a specific month. Returns api_calls, data_transfer_bytes, compute_seconds.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "year_month": {"type": "string", "description": "Month in YYYY-MM format"}}, "required": ["tenant_id", "year_month"]}},
    {"name": "get_usage_by_endpoint", "description": "Get usage breakdown for a specific API endpoint in a month",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "endpoint": {"type": "string", "description": "API endpoint path e.g. /api/users"}, "year_month": {"type": "string", "description": "Month in YYYY-MM format"}}, "required": ["tenant_id", "endpoint", "year_month"]}},
    {"name": "get_usage_trend", "description": "Get monthly usage trend over a date range. Use start_month and end_month in YYYY-MM format.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "start_month": {"type": "string", "description": "Start month YYYY-MM"}, "end_month": {"type": "string", "description": "End month YYYY-MM"}}, "required": ["tenant_id", "start_month", "end_month"]}},
    {"name": "get_usage_breakdown", "description": "Get API usage broken down by endpoint for a month. Shows each endpoint with its call count, data transfer, and compute time sorted by most used.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "year_month": {"type": "string", "description": "Month in YYYY-MM format"}, "breakdown": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "year_month", "breakdown"]}},
]
BILLING_TOOLS = [
    {"name": "generate_invoice", "description": "Generate a draft invoice for a specific month",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "year_month": {"type": "string", "description": "Month in YYYY-MM format"}}, "required": ["tenant_id", "year_month"]}},
    {"name": "get_invoice_history", "description": "Get all past invoices for a tenant. Only requires tenant_id, no other parameters.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "history": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "history"]}},
    {"name": "apply_credit", "description": "Apply a credit to tenant account",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "amount_cents": {"type": "integer", "description": "Credit amount in cents"}, "reason": {"type": "string", "description": "Reason for the credit"}}, "required": ["tenant_id", "amount_cents", "reason"]}},
    {"name": "get_balance", "description": "Get current billing balance for a tenant. Only requires tenant_id.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "balance": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "balance"]}},
    {"name": "confirm_invoice", "description": "Confirm a draft invoice and transition it to sent status",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "year_month": {"type": "string", "description": "Month in YYYY-MM format"}, "confirm": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "year_month", "confirm"]}},
]
ENTITLEMENT_TOOLS = [
    {"name": "get_current_plan", "description": "Get the current plan and entitlements for a tenant. Returns plan_id, api_call_limit, data_transfer_limit_gb.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "plan_info": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "plan_info"]}},
    {"name": "check_quota", "description": "Check quota usage vs plan limits. Returns usage percentages and is_approaching_limit flag.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "check_quota": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "check_quota"]}},
    {"name": "get_plan_catalog", "description": "List all available plans with pricing and limits. No tenant_id needed.",
     "inputSchema": {"type": "object", "properties": {"catalog": {"type": "boolean", "description": "Must be true"}}, "required": ["catalog"]}},
    {"name": "recommend_upgrade", "description": "Analyze current usage and recommend the best plan upgrade based on headroom.",
     "inputSchema": {"type": "object", "properties": {"tenant_id": {"type": "string"}, "recommend": {"type": "boolean", "description": "Must be true"}}, "required": ["tenant_id", "recommend"]}},
]


def _tool_def(t: dict) -> agentcore.CfnGatewayTarget.ToolDefinitionProperty:
    schema = t["inputSchema"]
    props = {k: agentcore.CfnGatewayTarget.SchemaDefinitionProperty(type=v.get("type", "string"))
             for k, v in schema.get("properties", {}).items()}
    req = schema.get("required", [])
    return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
        name=t["name"], description=t["description"],
        input_schema=agentcore.CfnGatewayTarget.SchemaDefinitionProperty(
            type="object", properties=props, required=req if req else None),
    )


class UnifiedStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. DynamoDB ─────────────────────────────────────────────
        tables = {}
        for name in ["UsageRecords", "BillingRecords", "Entitlements", "PlanCatalog"]:
            tables[name] = dynamodb.Table(self, name,
                partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                removal_policy=RemovalPolicy.DESTROY)

        # ── 2. Cognito ──────────────────────────────────────────────
        pre_token_fn = _lambda.Function(self, "PreTokenFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/pre_token_generation"))

        user_pool = cognito.UserPool(self, "UserPool",
            user_pool_name="SaaSBillingUserPool",
            self_sign_up_enabled=False, removal_policy=RemovalPolicy.DESTROY,
            custom_attributes={"tenant_id": cognito.StringAttribute(min_len=1, max_len=256, mutable=True)},
            lambda_triggers=cognito.UserPoolTriggers(pre_token_generation=pre_token_fn))

        billing_scope = cognito.ResourceServerScope(scope_name="billing", scope_description="Billing tools")
        rs = user_pool.add_resource_server("ResourceServer", identifier="saas-billing", scopes=[billing_scope])

        m2m_client = user_pool.add_client("M2MClient",
            user_pool_client_name="SaaSBillingM2MClient", generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(rs, billing_scope)]))

        user_pool.add_domain("Domain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"saas-billing-{cdk.Aws.ACCOUNT_ID}"))

        frontend_client = user_pool.add_client("FrontendClient",
            user_pool_client_name="SaaSBillingFrontendClient", generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True))

        issuer_url = f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}/.well-known/openid-configuration"

        m2m_secret = secretsmanager.Secret(self, "M2MClientSecret",
            secret_string_value=m2m_client.user_pool_client_secret,
            description="SaaS Billing M2M client secret")

        # ── 3. Lambdas ──────────────────────────────────────────────
        usage_fn = _lambda.Function(self, "UsageFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/usage_service"),
            environment={"USAGE_RECORDS_TABLE": tables["UsageRecords"].table_name})
        usage_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["dynamodb:Query", "dynamodb:GetItem"], resources=[tables["UsageRecords"].table_arn]))

        billing_fn = _lambda.Function(self, "BillingFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/billing_service"),
            environment={"BILLING_RECORDS_TABLE": tables["BillingRecords"].table_name})
        billing_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
            resources=[tables["BillingRecords"].table_arn]))

        entitlement_fn = _lambda.Function(self, "EntitlementFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/entitlement_service"),
            environment={"ENTITLEMENTS_TABLE": tables["Entitlements"].table_name,
                         "PLAN_CATALOG_TABLE": tables["PlanCatalog"].table_name,
                         "USAGE_RECORDS_TABLE": tables["UsageRecords"].table_name})
        entitlement_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["dynamodb:Query", "dynamodb:GetItem"],
            resources=[tables["Entitlements"].table_arn, tables["PlanCatalog"].table_arn, tables["UsageRecords"].table_arn]))
        entitlement_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["dynamodb:Scan"], resources=[tables["PlanCatalog"].table_arn]))

        # Allow AgentCore Gateway to invoke Lambdas via resource-based policy
        for fn in [usage_fn, billing_fn, entitlement_fn]:
            fn.add_permission("GatewayInvoke",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                action="lambda:InvokeFunction")

        # ── 4. AgentCore IAM Role ───────────────────────────────────
        agentcore_resource_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"

        # ECR repo (created early so IAM role can reference it)
        ecr_repo = ecr.Repository(self, "ECR",
            repository_name=f"{self.stack_name.lower()}-agent",
            image_tag_mutability=ecr.TagMutability.MUTABLE,
            removal_policy=RemovalPolicy.DESTROY, empty_on_delete=True)

        agent_role = iam.Role(self, "AgentRole",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com")))

        # AgentCore: gateway, memory, code interpreter, runtime, identity — scoped to this account
        agent_role.add_to_policy(iam.PolicyStatement(sid="AgentCore", effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:*"],
            resources=[agentcore_resource_arn]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="Bedrock", effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:{self.region}:{self.account}:*"]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="Lambda", effect=iam.Effect.ALLOW,
            actions=["lambda:InvokeFunction"],
            resources=[usage_fn.function_arn, billing_fn.function_arn, entitlement_fn.function_arn]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="Logs", effect=iam.Effect.ALLOW,
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="XRay", effect=iam.Effect.ALLOW,
            actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                     "xray:GetSamplingRules", "xray:GetSamplingTargets"],
            resources=["*"]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="Secrets", effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[m2m_secret.secret_arn]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="ECRAuth", effect=iam.Effect.ALLOW,
            actions=["ecr:GetAuthorizationToken"],
            resources=["*"]))
        agent_role.add_to_policy(iam.PolicyStatement(sid="ECRPull", effect=iam.Effect.ALLOW,
            actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
            resources=[ecr_repo.repository_arn]))

        # ── 5. AgentCore Memory ─────────────────────────────────────
        memory = agentcore.CfnMemory(self, "Memory",
            name="saas_billing_memory", event_expiry_duration=90,
            description="SaaS Billing Agent Memory",
            memory_strategies=[
                agentcore.CfnMemory.MemoryStrategyProperty(
                    semantic_memory_strategy=agentcore.CfnMemory.SemanticMemoryStrategyProperty(
                        name="fact_extractor", description="Extracts and stores factual information about tenants",
                        namespaces=["/facts/{actorId}"])),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    user_preference_memory_strategy=agentcore.CfnMemory.UserPreferenceMemoryStrategyProperty(
                        name="preference_learner", description="Learns tenant display and billing preferences",
                        namespaces=["/preferences/{actorId}"])),
                agentcore.CfnMemory.MemoryStrategyProperty(
                    summary_memory_strategy=agentcore.CfnMemory.SummaryMemoryStrategyProperty(
                        name="session_summarizer", description="Summarizes billing conversation sessions",
                        namespaces=["/summaries/{actorId}/{sessionId}"])),
            ])

        # ── 6. Workload Identity ────────────────────────────────────
        identity = agentcore.CfnWorkloadIdentity(self, "Identity", name="saas_billing_identity")

        # ── 7. Code Interpreter ─────────────────────────────────────
        code_interp = agentcore.CfnCodeInterpreterCustom(self, "CodeInterpreter",
            name="saas_billing_code_interpreter", description="Analytics and charts",
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode="PUBLIC"))

        # ── 8. OAuth2 Credential Provider (Custom Resource) ─────────
        # Lambda layer with latest boto3 (Lambda runtime's bundled version is too old for AgentCore APIs)
        # Layer is built from .layers/boto3/python/ — run: pip install boto3 botocore -t .layers/boto3/python
        boto3_layer_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".layers", "boto3")
        boto3_layer = _lambda.LayerVersion(self, "Boto3Layer",
            code=_lambda.Code.from_asset(boto3_layer_path),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Latest boto3/botocore for AgentCore control plane APIs")

        cp_fn = _lambda.Function(self, "CredProviderFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.minutes(5),
            layers=[boto3_layer],
            code=_lambda.Code.from_inline(CRED_PROVIDER_LAMBDA_CODE))
        cp_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:CreateOauth2CredentialProvider",
                     "bedrock-agentcore:GetOauth2CredentialProvider",
                     "bedrock-agentcore:DeleteOauth2CredentialProvider"],
            resources=[agentcore_resource_arn]))
        cp_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[m2m_secret.secret_arn]))

        cred_provider = CustomResource(self, "CredProvider",
            service_token=cp_fn.function_arn,
            properties={"Name": "SaaSBillingCredentialProvider", "DiscoveryUrl": issuer_url,
                        "ClientId": m2m_client.user_pool_client_id,
                        "ClientSecretArn": m2m_secret.secret_arn,
                        "Region": self.region})

        # ── 9. MCP Gateway + Targets ────────────────────────────────
        gateway = agentcore.CfnGateway(self, "Gateway",
            name="SaaSBillingGateway", protocol_type="MCP",
            authorizer_type="CUSTOM_JWT", role_arn=agent_role.role_arn,
            authorizer_configuration=agentcore.CfnGateway.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=issuer_url,
                    allowed_clients=[m2m_client.user_pool_client_id])),
            description="MCP Gateway for billing tools")
        gateway.add_dependency(agent_role.node.default_child)

        for name, fn, tools in [("UsageService", usage_fn, USAGE_TOOLS),
                                 ("BillingService", billing_fn, BILLING_TOOLS),
                                 ("EntitlementService", entitlement_fn, ENTITLEMENT_TOOLS)]:
            agentcore.CfnGatewayTarget(self, f"{name}Target", name=name,
                gateway_identifier=gateway.attr_gateway_identifier,
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=[_tool_def(t) for t in tools])))),
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE")])

        # ── 9a. Policy Engine + Cedar Policy + Gateway Attachment ──
        # Step 1: Create Policy Engine
        policy_engine = agentcore.CfnPolicyEngine(self, "PolicyEngine",
            name="saas_billing_policy_engine",
            description="Authorization policies for SaaS Billing Gateway")

        # Step 2+3: Create Cedar policy and attach engine to gateway via Custom Resource
        policy_fn = _lambda.Function(self, "PolicyFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.minutes(5),
            layers=[boto3_layer],
            code=_lambda.Code.from_inline(POLICY_LAMBDA_CODE))
        policy_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:*", "bedrock-agentcore-control:*"],
            resources=["*"]))

        policy_cr = CustomResource(self, "PolicySetup",
            service_token=policy_fn.function_arn,
            properties={
                "PolicyEngineId": policy_engine.attr_policy_engine_id,
                "PolicyEngineArn": policy_engine.attr_policy_engine_arn,
                "GatewayId": gateway.attr_gateway_identifier,
                "GatewayArn": gateway.attr_gateway_arn,
                "Region": self.region,
                "Version": "4",
            })
        policy_cr.node.add_dependency(policy_engine)
        policy_cr.node.add_dependency(gateway)

        # ── 9b. Gateway + Memory Observability (CloudWatch Logs + Traces) ──
        obs_fn = _lambda.Function(self, "ObservabilityFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.minutes(5),
            code=_lambda.Code.from_inline(OBSERVABILITY_LAMBDA_CODE))
        obs_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["logs:CreateLogGroup", "logs:PutDeliverySource", "logs:PutDeliveryDestination",
                     "logs:CreateDelivery", "logs:DeleteDeliverySource", "logs:DeleteDeliveryDestination",
                     "logs:DeleteDelivery", "logs:DescribeDeliveries",
                     "logs:GetDeliverySource", "logs:GetDeliveryDestination", "logs:GetDelivery"],
            resources=["*"]))
        obs_fn.add_to_role_policy(iam.PolicyStatement(effect=iam.Effect.ALLOW,
            actions=["bedrock-agentcore:AllowVendedLogDeliveryForResource"],
            resources=[agentcore_resource_arn]))

        gateway_obs = CustomResource(self, "GatewayObservability",
            service_token=obs_fn.function_arn,
            properties={
                "ResourceArn": gateway.attr_gateway_arn,
                "ResourceId": gateway.attr_gateway_identifier,
                "ResourceType": "gateway",
                "AccountId": self.account,
                "Region": self.region,
            })
        gateway_obs.node.add_dependency(gateway)

        memory_obs = CustomResource(self, "MemoryObservability",
            service_token=obs_fn.function_arn,
            properties={
                "ResourceArn": memory.attr_memory_arn,
                "ResourceId": memory.attr_memory_id,
                "ResourceType": "memory",
                "AccountId": self.account,
                "Region": self.region,
            })
        memory_obs.node.add_dependency(memory)

        # ── 10. CodeBuild + Runtime (container deploy) ─────────────

        source_asset = s3_assets.Asset(self, "SourceAsset", path=".",
            exclude=["cdk.out", "node_modules", "frontend/node_modules",
                     "frontend/dist", "frontend", ".git", "__pycache__", "*.pyc",
                     "tests", "infra", "scripts", ".kiro", ".bedrock_agentcore.yaml",
                     ".bedrock_agentcore", "cdk.json", "*.md"])

        cb_role = iam.Role(self, "CodeBuildRole",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
            inline_policies={"cb": iam.PolicyDocument(statements=[
                iam.PolicyStatement(effect=iam.Effect.ALLOW,
                    actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/codebuild/*"]),
                iam.PolicyStatement(effect=iam.Effect.ALLOW,
                    actions=["ecr:GetAuthorizationToken"],
                    resources=["*"]),
                iam.PolicyStatement(effect=iam.Effect.ALLOW,
                    actions=["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer",
                             "ecr:BatchGetImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                             "ecr:CompleteLayerUpload", "ecr:PutImage"],
                    resources=[ecr_repo.repository_arn]),
                iam.PolicyStatement(effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject"], resources=[f"{source_asset.bucket.bucket_arn}/*"]),
            ])})

        build_project = codebuild.Project(self, "BuildProject",
            project_name=f"{self.stack_name}-agent-build",
            role=cb_role,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE, privileged=True),
            source=codebuild.Source.s3(bucket=source_asset.bucket, path=source_asset.s3_object_key),
            environment_variables={
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(value=self.account),
                "IMAGE_REPO_NAME": codebuild.BuildEnvironmentVariable(value=ecr_repo.repository_name),
                "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value="latest"),
            },
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {"commands": [
                        "aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
                    ]},
                    "build": {"commands": [
                        "docker build -t $IMAGE_REPO_NAME:$IMAGE_TAG .",
                        "docker tag $IMAGE_REPO_NAME:$IMAGE_TAG $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:$IMAGE_TAG",
                    ]},
                    "post_build": {"commands": [
                        "docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:$IMAGE_TAG",
                    ]},
                },
            }))

        # Custom Resource to trigger CodeBuild and wait for completion
        build_trigger_fn = _lambda.Function(self, "BuildTriggerFn",
            runtime=_lambda.Runtime.PYTHON_3_12, handler="index.handler",
            timeout=Duration.minutes(15),
            code=_lambda.Code.from_inline(BUILD_TRIGGER_LAMBDA_CODE),
            initial_policy=[iam.PolicyStatement(effect=iam.Effect.ALLOW,
                actions=["codebuild:StartBuild", "codebuild:BatchGetBuilds"],
                resources=[build_project.project_arn])])

        trigger_build = CustomResource(self, "TriggerBuild",
            service_token=build_trigger_fn.function_arn,
            properties={"ProjectName": build_project.project_name,
                         "SourceHash": source_asset.asset_hash})

        # AgentCore Runtime (container)
        runtime = agentcore.CfnRuntime(self, "Runtime",
            agent_runtime_name="saas_billing_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{ecr_repo.repository_uri}:latest")),
            role_arn=agent_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
            protocol_configuration="HTTP",
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=issuer_url,
                    allowed_clients=[frontend_client.user_pool_client_id])),
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=900, max_lifetime=3600),
            request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=["Authorization", "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id"]),
            environment_variables={
                "AGENTCORE_GATEWAY_URL": gateway.attr_gateway_url,
                "MEMORY_ID": memory.attr_memory_id,
                "CODE_INTERPRETER_ID": code_interp.attr_code_interpreter_id,
                "CREDENTIAL_PROVIDER_NAME": "SaaSBillingCredentialProvider",
                "COGNITO_DOMAIN": f"https://saas-billing-{cdk.Aws.ACCOUNT_ID}.auth.{self.region}.amazoncognito.com",
                "GATEWAY_CLIENT_ID": m2m_client.user_pool_client_id,
                "GATEWAY_CLIENT_SECRET_ARN": m2m_secret.secret_arn,
                "GATEWAY_SCOPE": "saas-billing/billing",
                "AWS_DEFAULT_REGION": self.region,
                "MODEL_ID": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "SOURCE_HASH": source_asset.asset_hash},
            description="SaaS Billing Agent")
        runtime.node.add_dependency(trigger_build)
        runtime.node.add_dependency(cred_provider)

        # Note: AgentCore automatically creates a DEFAULT endpoint.
        # No need to create an additional one.

        # ── 11. Frontend (S3 + CloudFront) ──────────────────────────
        bucket = s3.Bucket(self, "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        dist = cloudfront.Distribution(self, "CDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(http_status=403, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.seconds(0)),
                cloudfront.ErrorResponse(http_status=404, response_http_status=200,
                    response_page_path="/index.html", ttl=Duration.seconds(0))])

        s3deploy.BucketDeployment(self, "DeployFrontend",
            sources=[s3deploy.Source.asset("frontend/dist")],
            destination_bucket=bucket, distribution=dist, distribution_paths=["/*"])

        # ── 12. Outputs ────────────────────────────────────────────
        for k, v in {
            "UserPoolId": user_pool.user_pool_id,
            "FrontendClientId": frontend_client.user_pool_client_id,
            "M2MClientId": m2m_client.user_pool_client_id,
            "FrontendUrl": f"https://{dist.distribution_domain_name}",
            "GatewayUrl": gateway.attr_gateway_url,
            "GatewayId": gateway.attr_gateway_identifier,
            "MemoryId": memory.attr_memory_id,
            "CodeInterpreterId": code_interp.attr_code_interpreter_id,
            "IdentityArn": identity.attr_workload_identity_arn,
            "PolicyEngineId": policy_engine.attr_policy_engine_id,
            "RuntimeId": runtime.attr_agent_runtime_id,
            "RuntimeArn": runtime.attr_agent_runtime_arn,
            "AgentRoleArn": agent_role.role_arn,
            "UsageServiceFunctionArn": usage_fn.function_arn,
            "BillingServiceFunctionArn": billing_fn.function_arn,
            "EntitlementServiceFunctionArn": entitlement_fn.function_arn,
            "CognitoDomain": f"https://saas-billing-{cdk.Aws.ACCOUNT_ID}.auth.{self.region}.amazoncognito.com",
        }.items():
            cdk.CfnOutput(self, k, value=v)
        for name, tbl in tables.items():
            cdk.CfnOutput(self, f"{name}Table", value=tbl.table_name)


# ── Lambda code for Custom Resources ───────────────────────────────

CRED_PROVIDER_LAMBDA_CODE = '''
import boto3, json, logging, urllib3
logger = logging.getLogger()
logger.setLevel(logging.INFO)
http = urllib3.PoolManager()

def send(event, context, status, data=None, reason=None):
    body = json.dumps({"Status": status,
        "Reason": reason or f"See logs: {context.log_stream_name}",
        "PhysicalResourceId": (data or {}).get("Name", context.log_stream_name),
        "StackId": event["StackId"], "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}})
    http.request("PUT", event["ResponseURL"], body=body,
        headers={"content-type": "", "content-length": str(len(body))})

def _get_secret(secret_arn, region):
    sm = boto3.client("secretsmanager", region_name=region)
    return sm.get_secret_value(SecretId=secret_arn)["SecretString"]

def handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    props = event.get("ResourceProperties", {})
    region = props.get("Region", "us-east-1")
    client = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        if event["RequestType"] == "Delete":
            try: client.delete_oauth2_credential_provider(name=props["Name"])
            except Exception: pass
            send(event, context, "SUCCESS", {"Name": props["Name"]})
            return
        try:
            existing = client.get_oauth2_credential_provider(name=props["Name"])
            send(event, context, "SUCCESS", {"Name": props["Name"],
                "Arn": existing.get("credentialProviderArn", ""),
                "CallbackUrl": existing.get("callbackUrl", "")})
            return
        except Exception: pass
        client_secret = _get_secret(props["ClientSecretArn"], region)
        resp = client.create_oauth2_credential_provider(name=props["Name"],
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={"customOauth2ProviderConfig": {
                "oauthDiscovery": {"discoveryUrl": props["DiscoveryUrl"]},
                "clientId": props["ClientId"], "clientSecret": client_secret}})
        send(event, context, "SUCCESS", {"Name": props["Name"],
            "Arn": resp.get("credentialProviderArn", ""),
            "CallbackUrl": resp.get("callbackUrl", "")})
    except Exception as e:
        logger.error("Error: %s", e)
        send(event, context, "FAILED", reason=str(e))
'''

BUILD_TRIGGER_LAMBDA_CODE = '''
import boto3, json, logging, time, urllib3
logger = logging.getLogger()
logger.setLevel(logging.INFO)
http = urllib3.PoolManager()

def send(event, context, status, data=None, reason=None):
    body = json.dumps({"Status": status,
        "Reason": reason or f"See logs: {context.log_stream_name}",
        "PhysicalResourceId": (data or {}).get("BuildId", context.log_stream_name),
        "StackId": event["StackId"], "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}})
    http.request("PUT", event["ResponseURL"], body=body,
        headers={"content-type": "", "content-length": str(len(body))})

def handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    if event["RequestType"] == "Delete":
        send(event, context, "SUCCESS")
        return
    try:
        project = event["ResourceProperties"]["ProjectName"]
        cb = boto3.client("codebuild")
        resp = cb.start_build(projectName=project)
        build_id = resp["build"]["id"]
        logger.info("Started build: %s", build_id)
        max_wait = context.get_remaining_time_in_millis() / 1000 - 30
        start = time.time()
        while True:
            if time.time() - start > max_wait:
                send(event, context, "FAILED", reason="Build timeout")
                return
            r = cb.batch_get_builds(ids=[build_id])
            status = r["builds"][0]["buildStatus"]
            if status == "SUCCEEDED":
                send(event, context, "SUCCESS", {"BuildId": build_id})
                return
            if status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
                send(event, context, "FAILED", reason=f"Build {status}")
                return
            time.sleep(30)
    except Exception as e:
        logger.error("Error: %s", e)
        send(event, context, "FAILED", reason=str(e))
'''

OBSERVABILITY_LAMBDA_CODE = '''
import boto3, json, logging, urllib3
logger = logging.getLogger()
logger.setLevel(logging.INFO)
http = urllib3.PoolManager()

def send(event, context, status, data=None, reason=None):
    body = json.dumps({"Status": status,
        "Reason": reason or f"See logs: {context.log_stream_name}",
        "PhysicalResourceId": (data or {}).get("Id", context.log_stream_name),
        "StackId": event["StackId"], "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}})
    http.request("PUT", event["ResponseURL"], body=body,
        headers={"content-type": "", "content-length": str(len(body))})

def handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    props = event.get("ResourceProperties", {})
    region = props.get("Region", "us-east-1")
    account_id = props.get("AccountId")
    resource_arn = props.get("ResourceArn")
    resource_id = props.get("ResourceId")
    resource_type = props.get("ResourceType", "gateway")
    logs = boto3.client("logs", region_name=region)

    try:
        if event["RequestType"] == "Delete":
            # Clean up delivery sources and deliveries
            for suffix in ["logs", "traces"]:
                src_name = f"{resource_id}-{suffix}-source"
                try: logs.delete_delivery_source(name=src_name)
                except Exception: pass
                dest_name = f"{resource_id}-{suffix}-destination"
                try:
                    # Find and delete deliveries first
                    deliveries = logs.describe_deliveries().get("deliveries", [])
                    for d in deliveries:
                        if d.get("deliverySourceName") == src_name:
                            try: logs.delete_delivery(id=d["id"])
                            except Exception: pass
                except Exception: pass
                try: logs.delete_delivery_destination(name=dest_name)
                except Exception: pass
            send(event, context, "SUCCESS", {"Id": resource_id})
            return

        # Create or update: set up log delivery
        log_group_name = f"/aws/vendedlogs/bedrock-agentcore/{resource_type}/APPLICATION_LOGS/{resource_id}"
        try: logs.create_log_group(logGroupName=log_group_name)
        except logs.exceptions.ResourceAlreadyExistsException: pass
        log_group_arn = f"arn:aws:logs:{region}:{account_id}:log-group:{log_group_name}"

        # Logs delivery source
        src_name = f"{resource_id}-logs-source"
        try:
            logs.put_delivery_source(name=src_name, logType="APPLICATION_LOGS", resourceArn=resource_arn)
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("put_delivery_source logs: %s", e)

        # Logs delivery destination
        dest_name = f"{resource_id}-logs-destination"
        try:
            logs.put_delivery_destination(name=dest_name, deliveryDestinationType="CWL",
                deliveryDestinationConfiguration={"destinationResourceArn": log_group_arn})
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("put_delivery_destination logs: %s", e)

        # Create logs delivery
        try:
            logs.create_delivery(deliverySourceName=src_name, deliveryDestinationArn=f"arn:aws:logs:{region}:{account_id}:delivery-destination:{dest_name}")
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("create_delivery logs: %s", e)

        # Traces delivery source
        traces_src = f"{resource_id}-traces-source"
        try:
            logs.put_delivery_source(name=traces_src, logType="TRACES", resourceArn=resource_arn)
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("put_delivery_source traces: %s", e)

        # Traces delivery destination (X-Ray)
        traces_dest = f"{resource_id}-traces-destination"
        try:
            logs.put_delivery_destination(name=traces_dest, deliveryDestinationType="XRAY")
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("put_delivery_destination traces: %s", e)

        # Create traces delivery
        try:
            logs.create_delivery(deliverySourceName=traces_src,
                deliveryDestinationArn=f"arn:aws:logs:{region}:{account_id}:delivery-destination:{traces_dest}")
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("create_delivery traces: %s", e)

        send(event, context, "SUCCESS", {"Id": resource_id, "LogGroup": log_group_name})
    except Exception as e:
        logger.error("Error: %s", e)
        send(event, context, "FAILED", reason=str(e))
'''

POLICY_LAMBDA_CODE = '''
import boto3, json, logging, time, urllib3
logger = logging.getLogger()
logger.setLevel(logging.INFO)
http = urllib3.PoolManager()

def send(event, context, status, data=None, reason=None):
    body = json.dumps({"Status": status,
        "Reason": reason or f"See logs: {context.log_stream_name}",
        "PhysicalResourceId": (data or {}).get("PolicyId", context.log_stream_name),
        "StackId": event["StackId"], "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"], "Data": data or {}})
    http.request("PUT", event["ResponseURL"], body=body,
        headers={"content-type": "", "content-length": str(len(body))})

def handler(event, context):
    logger.info("Event: %s", json.dumps(event))
    props = event.get("ResourceProperties", {})
    region = props.get("Region", "us-east-1")
    engine_id = props.get("PolicyEngineId")
    engine_arn = props.get("PolicyEngineArn")
    gateway_id = props.get("GatewayId")
    gateway_arn = props.get("GatewayArn")
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    try:
        if event["RequestType"] == "Delete":
            try:
                gw = client.get_gateway(gatewayIdentifier=gateway_id)
                client.update_gateway(gatewayIdentifier=gateway_id,
                    name=gw["name"], roleArn=gw["roleArn"],
                    protocolType=gw["protocolType"], authorizerType=gw["authorizerType"],
                    authorizerConfiguration=gw.get("authorizerConfiguration", {}))
                logger.info("Detached policy engine")
            except Exception as e:
                logger.info("Detach: %s", e)
            try:
                policies = client.list_policies(policyEngineId=engine_id).get("policies", [])
                for p in policies:
                    try: client.delete_policy(policyEngineId=engine_id, policyId=p["policyId"])
                    except Exception: pass
            except Exception as e:
                logger.info("Delete policies: %s", e)
            send(event, context, "SUCCESS", {"PolicyId": "deleted"})
            return

        policy_name = "permit_billing_tools"
        cedar = "permit(principal is AgentCore::OAuthUser, action, resource is AgentCore::Gateway);"
        logger.info("Cedar: %s", cedar)

        policy_id = None
        try:
            policies = client.list_policies(policyEngineId=engine_id).get("policies", [])
            for p in policies:
                if p.get("name") == policy_name and p.get("status") == "ACTIVE":
                    policy_id = p["policyId"]
                    logger.info("Policy active: %s", policy_id)
                    break
                elif p.get("name") == policy_name:
                    try: client.delete_policy(policyEngineId=engine_id, policyId=p["policyId"])
                    except Exception: pass
        except Exception as e:
            logger.info("list: %s", e)

        if not policy_id:
            resp = client.create_policy(policyEngineId=engine_id, name=policy_name,
                definition={"cedar": {"statement": cedar}},
                description="Permit authenticated users to call any billing tool",
                validationMode="IGNORE_ALL_FINDINGS")
            policy_id = resp.get("policyId", "")
            logger.info("Created: %s", policy_id)
            for _ in range(6):
                time.sleep(5)
                p = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
                if p.get("status") == "ACTIVE":
                    break

        try:
            gw = client.get_gateway(gatewayIdentifier=gateway_id)
            client.update_gateway(gatewayIdentifier=gateway_id,
                name=gw["name"], roleArn=gw["roleArn"],
                protocolType=gw["protocolType"], authorizerType=gw["authorizerType"],
                authorizerConfiguration=gw.get("authorizerConfiguration", {}),
                policyEngineConfiguration={"arn": engine_arn, "mode": "ENFORCE"})
            logger.info("Attached policy engine (ENFORCE)")
        except Exception as e:
            logger.warning("Attach: %s", e)

        send(event, context, "SUCCESS", {"PolicyId": policy_id or "done"})
    except Exception as e:
        logger.error("Error: %s", e)
        send(event, context, "FAILED", reason=str(e))
'''
