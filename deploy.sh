#!/usr/bin/env bash
# =============================================================================
# SaaS Billing Agent — One-Command Deployment
#
# CDK creates ALL resources (DynamoDB, Cognito, Lambdas, IAM, ECR/CodeBuild,
# AgentCore Runtime/Gateway/Memory/Identity/CodeInterpreter, S3+CloudFront).
#
# This script orchestrates: Frontend build → CDK deploy → Frontend rebuild → Seed
#
# Usage:
#   bash deploy.sh              # Deploy without seeding data
#   bash deploy.sh --seed       # Deploy and seed sample data
#   bash deploy.sh --destroy    # Tear down everything
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="us-east-1"
# Force all AWS/CDK commands to use us-east-1
export AWS_DEFAULT_REGION="us-east-1"
export AWS_REGION="us-east-1"
export CDK_DEFAULT_REGION="us-east-1"
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1
AGENT_NAME="saas_billing_agent"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[deploy]${NC} $1"; }
ok()   { echo -e "${GREEN}  ✅ $1${NC}"; }
warn() { echo -e "${YELLOW}  ⚠️  $1${NC}"; }
fail() { echo -e "${RED}  ❌ $1${NC}"; exit 1; }

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
SEED_DATA=false
DESTROY=false
for arg in "$@"; do
  case "$arg" in
    --seed)    SEED_DATA=true ;;
    --destroy) DESTROY=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
preflight() {
  log "Preflight checks..."
  command -v aws    >/dev/null 2>&1 || fail "AWS CLI not found"
  command -v cdk    >/dev/null 2>&1 || fail "CDK CLI not found"
  command -v python3 >/dev/null 2>&1 || fail "python3 not found"
  command -v npm    >/dev/null 2>&1 || fail "npm not found"

  ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
    || fail "AWS credentials not configured"
  ok "AWS account: $ACCOUNT, region: $REGION"
}

# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------
destroy() {
  log "Destroying all resources..."
  log "Destroying CDK stack (includes all AgentCore resources)..."
  cdk destroy --force 2>/dev/null || warn "CDK stack not found"
  ok "All resources destroyed."
  exit 0
}

[ "$DESTROY" = true ] && destroy

# ---------------------------------------------------------------------------
# Step 1: Build frontend (placeholder — will rebuild with real config later)
# ---------------------------------------------------------------------------
step_build_frontend_placeholder() {
  log "Step 1: Building frontend (placeholder)..."
  (cd "$SCRIPT_DIR/frontend" && npm install --silent 2>/dev/null && ./node_modules/.bin/vite build 2>/dev/null)
  ok "Frontend built"
}

# ---------------------------------------------------------------------------
# Step 2: CDK deploy (creates ALL infrastructure including AgentCore resources)
# ---------------------------------------------------------------------------
step_cdk_deploy() {
  log "Step 2: Deploying CDK infrastructure (all resources)..."
  cdk bootstrap aws://"$ACCOUNT"/"$REGION" 2>/dev/null || true
  cdk deploy --require-approval never 2>&1 | grep -E "(✅|Outputs|Error|CREATE_FAILED)" || true

  # Extract outputs
  OUTPUTS=$(aws cloudformation describe-stacks --stack-name SaaSBillingStack \
    --query "Stacks[0].Outputs" --output json --region "$REGION")

  _out() { echo "$OUTPUTS" | python3 -c "import json,sys; o={x['OutputKey']:x['OutputValue'] for x in json.load(sys.stdin)}; print(o['$1'])"; }

  USER_POOL_ID=$(_out UserPoolId)
  M2M_CLIENT_ID=$(_out M2MClientId)
  FRONTEND_CLIENT_ID=$(_out FrontendClientId)
  ROLE_ARN=$(_out AgentRoleArn)
  FRONTEND_URL=$(_out FrontendUrl)
  RUNTIME_ARN=$(_out RuntimeArn)
  COGNITO_DOMAIN=$(_out CognitoDomain)
  GATEWAY_ID=$(_out GatewayId)
  MEMORY_ID=$(_out MemoryId)
  ROLE_NAME=$(echo "$ROLE_ARN" | awk -F'/' '{print $NF}')

  ok "CDK deployed — UserPool: $USER_POOL_ID, Frontend: $FRONTEND_URL"
  ok "Runtime: $RUNTIME_ARN"
  ok "Gateway: $GATEWAY_ID, Memory: $MEMORY_ID"
}

# ---------------------------------------------------------------------------
# Step 3: Verify IAM role
# ---------------------------------------------------------------------------
step_verify_iam() {
  log "Step 3: Verifying IAM role..."
  aws iam get-role --role-name "$ROLE_NAME" --region "$REGION" >/dev/null 2>&1 \
    || fail "Agent role $ROLE_NAME not found — CDK deploy may have failed"
  ok "IAM role verified: $ROLE_NAME"
}

# ---------------------------------------------------------------------------
# Step 4: Rebuild frontend with real config + push to S3
# ---------------------------------------------------------------------------
step_rebuild_frontend() {
  log "Step 4: Rebuilding frontend with real Cognito config..."

  cat > "$SCRIPT_DIR/frontend/.env" << EOF
VITE_COGNITO_USER_POOL_ID=$USER_POOL_ID
VITE_COGNITO_CLIENT_ID=$FRONTEND_CLIENT_ID
VITE_COGNITO_DOMAIN=$COGNITO_DOMAIN
VITE_REDIRECT_SIGN_IN=$FRONTEND_URL/
VITE_REDIRECT_SIGN_OUT=$FRONTEND_URL/
VITE_AGENT_RUNTIME_URL=https://bedrock-agentcore.$REGION.amazonaws.com
VITE_AGENT_RUNTIME_ARN=$RUNTIME_ARN
EOF

  (cd "$SCRIPT_DIR/frontend" && ./node_modules/.bin/vite build 2>/dev/null)

  # Get S3 bucket and CloudFront distribution
  S3_BUCKET=$(aws cloudformation describe-stack-resources --stack-name SaaSBillingStack \
    --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId" \
    --output text --region "$REGION" | head -1)
  CF_DIST=$(aws cloudformation describe-stack-resources --stack-name SaaSBillingStack \
    --query "StackResources[?ResourceType=='AWS::CloudFront::Distribution'].PhysicalResourceId" \
    --output text --region "$REGION")

  aws s3 sync "$SCRIPT_DIR/frontend/dist/" "s3://$S3_BUCKET/" --delete --region "$REGION" >/dev/null
  aws cloudfront create-invalidation --distribution-id "$CF_DIST" --paths "/*" --region us-east-1 >/dev/null 2>&1

  ok "Frontend deployed to $FRONTEND_URL"
}

# ---------------------------------------------------------------------------
# Step 5: Seed data (optional)
# ---------------------------------------------------------------------------
step_seed_data() {
  if [ "$SEED_DATA" = true ]; then
    log "Step 5: Seeding DynamoDB tables..."
    USAGE_RECORDS_TABLE=$(_out UsageRecordsTable) \
    BILLING_RECORDS_TABLE=$(_out BillingRecordsTable) \
    ENTITLEMENTS_TABLE=$(_out EntitlementsTable) \
    PLAN_CATALOG_TABLE=$(_out PlanCatalogTable) \
      python3 "$SCRIPT_DIR/scripts/seed_data.py"
    ok "Data seeded (2025 + 2026)"
  else
    log "Step 5: Skipping data seeding (use --seed to enable)"
  fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
  echo ""
  echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
  echo -e "${GREEN}  SaaS Billing Agent — Deployment Complete${NC}"
  echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
  echo ""
  echo -e "  Frontend:     ${CYAN}$FRONTEND_URL${NC}"
  echo -e "  Agent:        ${CYAN}$AGENT_NAME${NC}"
  echo -e "  Runtime:      ${CYAN}$RUNTIME_ARN${NC}"
  echo -e "  Gateway:      ${CYAN}$GATEWAY_ID${NC}"
  echo -e "  Memory:       ${CYAN}$MEMORY_ID${NC}"
  echo -e "  User Pool:    ${CYAN}$USER_POOL_ID${NC}"
  echo -e "  Region:       ${CYAN}$REGION${NC}"
  echo ""
  echo -e "  Test with:"
  echo -e "    ${YELLOW}agentcore invoke '{\"prompt\": \"What is my usage?\"}' \\${NC}"
  echo -e "    ${YELLOW}  --headers \"X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id:tenant-alpha\"${NC}"
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  preflight
  step_build_frontend_placeholder
  step_cdk_deploy
  step_verify_iam
  step_rebuild_frontend
  step_seed_data
  print_summary
}

main
