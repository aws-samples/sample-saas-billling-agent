# SaaS Billing Agent

>DISCLAIMER: This code is provided as a sample for educational and testing purposes only. Users must perform their own security review and due diligence before deploying any code to production environments. The code provided represents a baseline implementation and may not address all security considerations for your specific environment.

A multi-tenant SaaS billing agent built on **Amazon Bedrock AgentCore**. Tenants interact through a chat interface to query API usage, manage invoices, check entitlements, and get plan recommendations — all powered by Claude on Bedrock.

## Architecture

```
Browser (React) → CloudFront/S3 → Cognito Auth → AgentCore Runtime (Docker)
                                                        ↓
                                              Strands Agent (Claude Haiku 4.5)
                                              ↓         ↓           ↓
                                        Memory     MCP Gateway    Code Interpreter
                                      (STM+LTM)   (Policy:ENFORCE)  (Charts)
                                                   (3 Lambdas)
                                                        ↓
                                                  DynamoDB (4 tables)
```

## Key Capabilities

- **Usage analytics** — per endpoint, monthly trends, growth percentages
- **Invoice management** — generation, history, credits, balances, confirmation
- **Entitlements** — quota checks, plan catalog, upgrade recommendations
- **Data visualizations** — bar charts, pie charts, cost projections via Code Interpreter
- **Conversation memory** — STM (within session) + LTM (semantic facts, preferences, summaries)
- **Policy enforcement** — Cedar-based authorization via AgentCore Policy Engine (ENFORCE mode)
- **Observability** — CloudWatch Logs + X-Ray traces for Runtime, Gateway, and Memory
- **Multi-tenant isolation** — enforced at JWT, DynamoDB, Memory, Gateway, and Policy layers

## Prerequisites

- **AWS CLI** configured with credentials (`aws configure`)
- **AWS CDK CLI** (`npm install -g aws-cdk`)
- **Python 3.12+** with pip
- **Node.js 18+** and npm
- **Amazon Bedrock** access enabled in `us-east-1` (Claude Haiku 4.5 model)
- **Bedrock AgentCore** access enabled

## Quick Start

```bash
# 1. Install CDK dependencies
pip install -r requirements-cdk.txt

# 2. Build boto3 Lambda layer (required for AgentCore Policy APIs)
mkdir -p .layers/boto3/python
pip install boto3 botocore -t .layers/boto3/python

# 3. Deploy everything
bash deploy.sh --seed
```

This single command:
1. Builds the React frontend
2. Deploys all infrastructure via CDK (DynamoDB, Cognito, Lambdas, ECR, AgentCore Runtime/Gateway/Memory/Policy/CodeInterpreter, S3+CloudFront)
3. Rebuilds frontend with real Cognito config and pushes to CloudFront
4. Seeds DynamoDB with sample data for two tenants (2025 + 2026)

After deploy completes, create test users:

```bash
POOL_ID=<UserPoolId from deploy output>

# tenant-alpha (Pro plan)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username tenant-alpha \
  --temporary-password TempPass123! --user-attributes Name=custom:tenant_id,Value=tenant-alpha \
  --message-action SUPPRESS --region us-east-1
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username tenant-alpha \
  --password TenantAlpha123! --permanent --region us-east-1

# tenant-beta (Starter plan)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID --username tenant-beta \
  --temporary-password TempPass123! --user-attributes Name=custom:tenant_id,Value=tenant-beta \
  --message-action SUPPRESS --region us-east-1
aws cognito-idp admin-set-user-password --user-pool-id $POOL_ID --username tenant-beta \
  --password TenantBeta123! --permanent --region us-east-1
```

## What Gets Deployed

| Resource | Purpose |
|---|---|
| 4 DynamoDB tables | Usage, Billing, Entitlements, Plan Catalog |
| Cognito User Pool | Auth (frontend JWT + M2M OAuth2 for Gateway) |
| 3 Lambda functions | Usage, Billing, Entitlement services (13 tools total) |
| ECR + CodeBuild | Builds and hosts agent container image |
| AgentCore Runtime | Runs the Strands agent container |
| AgentCore MCP Gateway | Routes tool calls to Lambdas (CUSTOM_JWT auth) |
| AgentCore Memory | Per-tenant STM + 3 LTM strategies (semantic, preferences, summaries) |
| AgentCore Code Interpreter | Chart generation (matplotlib, custom CDK-managed) |
| AgentCore Policy Engine | Cedar authorization policies (ENFORCE mode) |
| AgentCore Workload Identity | M2M credential provider for Gateway auth |
| CloudWatch Logs + X-Ray | Observability for Gateway and Memory |
| S3 + CloudFront | Frontend hosting |
| boto3 Lambda Layer | Latest SDK for Custom Resource Lambdas |

## Sample Tenants

| Tenant | Password | Plan | API Limit |
|---|---|---|---|
| `tenant-alpha` | `TenantAlpha123!` | Pro ($99/mo) | 100,000 calls/mo |
| `tenant-beta` | `TenantBeta123!` | Starter ($29/mo) | 10,000 calls/mo |

## MCP Gateway Tools (13 total)

| Service | Tools |
|---|---|
| UsageService | `get_usage_summary`, `get_usage_by_endpoint`, `get_usage_trend`, `get_usage_breakdown` |
| BillingService | `generate_invoice`, `get_invoice_history`, `apply_credit`, `get_balance`, `confirm_invoice` |
| EntitlementService | `get_current_plan`, `check_quota`, `get_plan_catalog`, `recommend_upgrade` |

## Auth Flow

1. User → Cognito login (USER_PASSWORD_AUTH) → access token
2. Frontend → Runtime `/runtimes/{arn}/invocations?qualifier=DEFAULT` with Bearer token
3. Runtime JWT authorizer validates `client_id` against FrontendClient
4. Agent → AgentCore Identity (`@requires_access_token`, M2M flow) → Cognito M2M token
5. Agent → MCP Gateway (Policy Engine evaluates Cedar policy) → Lambda → DynamoDB

## Updating the Agent

After changing agent code (`agent/` directory):

```bash
rm -rf cdk.out && cdk deploy --require-approval never
python3 scripts/update_runtime.py
```

## Updating the Frontend

```bash
cd frontend && npx vite build
aws s3 sync frontend/dist/ s3://$BUCKET/ --delete
aws cloudfront create-invalidation --distribution-id $DIST --paths "/*"
```

## Tear Down

```bash
bash deploy.sh --destroy
```

## Project Structure

```
├── agent/                  # Agent core (Strands + Memory + Code Interpreter)
│   ├── agent.py            # Main agent with auth, tools, image extraction
│   ├── access_token.py     # AgentCore Identity + Cognito fallback
│   └── memory_hook.py      # Memory hook (legacy, replaced by session manager)
├── frontend/               # React SPA (Vite + Amplify Auth + conversation history)
├── infra/                  # CDK infrastructure (single UnifiedStack)
│   └── stacks/unified_stack.py
├── lambdas/                # Lambda handlers with discriminator-based routing
│   ├── usage_service/
│   ├── billing_service/
│   ├── entitlement_service/
│   └── pre_token_generation/
├── scripts/                # Seed data + runtime update utility
├── deploy.sh               # One-click deploy orchestrator
├── Dockerfile              # Agent container
├── requirements.txt        # Container dependencies
└── requirements-cdk.txt    # CDK dependencies
```

## AI/GenAI Usage Disclosure

This solution uses Amazon Bedrock with Claude Haiku 4.5 to power the SaaS billing agent. Please note:

1. **AI-Generated Responses**: All billing insights, usage analytics, and plan recommendations are generated by AI and should be reviewed by qualified personnel before acting on them
2. **Suggestions, Not Guarantees**: The agent provides suggestions based on data analysis, not guaranteed outcomes or definitive financial advice
3. **Human Oversight Required**: Users maintain full responsibility for billing decisions and should validate all recommendations
4. **Data Processing**: The AI processes your tenant usage and billing data to generate insights, but does not store or share this data outside your AWS environment

## Bias and Fairness Considerations

This AI-powered billing agent is designed with the following considerations:

1. **Balanced Recommendations**: The agent analyzes all plan options equally and does not favor specific tiers in its upgrade recommendations
2. **Multi-Tenant Fairness**: The agent provides equitable analysis across all tenants without bias toward larger or smaller usage patterns
3. **Transparency**: All recommendations include clear explanations of the analysis methodology and data sources used
4. **Human Review Requirements**: Critical billing decisions require human oversight, especially for invoice generation, credit application, and plan changes
5. **Policy Enforcement**: Cedar-based authorization ensures tenants can only access their own data, preventing cross-tenant information leakage

## Contributing

We welcome contributions to improve the SaaS Billing Agent! Please follow these guidelines:

### Contributor License Agreement
By contributing to this project, you agree that your contributions will be licensed under the same Apache License 2.0 that covers this project. This ensures that the project remains open source and that all contributors' work is properly protected.

### How to Contribute
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

### Contribution Guidelines
- Follow existing code style and patterns
- Include comprehensive tests for new features
- Update documentation as needed
- Ensure all security best practices are followed

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/LICENSE) file for details.
