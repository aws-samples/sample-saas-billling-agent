#!/usr/bin/env bash
# End-to-end test script for the SaaS Billing Agent.
#
# Tests usage inquiry, quota check, invoice generation, credit application,
# and tenant isolation scenarios using `agentcore invoke`.
#
# Validates: Requirements 16.3, 16.4
#
# Prerequisites:
#   - Agent deployed via `agentcore launch`
#   - Seed data loaded via `python scripts/seed_data.py`
#   - AGENT_ID environment variable set
#   - TENANT_A_TOKEN and TENANT_B_TOKEN environment variables set (JWT tokens)
#
# Usage:
#   export AGENT_ID="your-agent-id"
#   export TENANT_A_TOKEN="jwt-for-tenant-alpha"
#   export TENANT_B_TOKEN="jwt-for-tenant-beta"
#   bash scripts/test_agent.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_ID="${AGENT_ID:?Error: AGENT_ID environment variable is required}"
TENANT_A_TOKEN="${TENANT_A_TOKEN:?Error: TENANT_A_TOKEN environment variable is required}"
TENANT_B_TOKEN="${TENANT_B_TOKEN:?Error: TENANT_B_TOKEN environment variable is required}"

PASS_COUNT=0
FAIL_COUNT=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log_test() {
    echo ""
    echo "================================================================"
    echo "TEST: $1"
    echo "================================================================"
}

invoke_agent() {
    local token="$1"
    local prompt="$2"
    agentcore invoke \
        --agent-id "$AGENT_ID" \
        --token "$token" \
        --payload "{\"input_text\": \"$prompt\"}" \
        2>&1
}

assert_contains() {
    local output="$1"
    local expected="$2"
    local test_name="$3"

    if echo "$output" | grep -qi "$expected"; then
        echo "  PASS: $test_name"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "  FAIL: $test_name (expected output to contain '$expected')"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

assert_not_contains() {
    local output="$1"
    local unexpected="$2"
    local test_name="$3"

    if echo "$output" | grep -qi "$unexpected"; then
        echo "  FAIL: $test_name (output should NOT contain '$unexpected')"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    else
        echo "  PASS: $test_name"
        PASS_COUNT=$((PASS_COUNT + 1))
    fi
}

# ---------------------------------------------------------------------------
# Test 1: Usage Inquiry
# ---------------------------------------------------------------------------

log_test "Usage Inquiry — Tenant Alpha, March 2025"
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "Show me my API usage summary for March 2025")
assert_contains "$RESPONSE" "api_calls" "Response includes api_calls"
assert_contains "$RESPONSE" "data_transfer" "Response includes data_transfer"
assert_contains "$RESPONSE" "2025-03" "Response references the requested month"

# ---------------------------------------------------------------------------
# Test 2: Quota Check
# ---------------------------------------------------------------------------

log_test "Quota Check — Tenant Alpha"
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "What is my current quota status?")
assert_contains "$RESPONSE" "quota" "Response mentions quota"
assert_contains "$RESPONSE" "limit" "Response mentions limit"

# ---------------------------------------------------------------------------
# Test 3: Invoice Generation (confirmation flow)
# ---------------------------------------------------------------------------

log_test "Invoice Generation — Tenant Alpha, April 2025"
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "Generate an invoice for April 2025")
assert_contains "$RESPONSE" "confirm" "Agent asks for confirmation before generating"

# Confirm the invoice
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "Yes, please confirm the invoice generation")
assert_contains "$RESPONSE" "invoice" "Response references the invoice"

# ---------------------------------------------------------------------------
# Test 4: Credit Application
# ---------------------------------------------------------------------------

log_test "Credit Application — Tenant Alpha"
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "Apply a credit of 2500 cents for service disruption")
assert_contains "$RESPONSE" "confirm" "Agent asks for confirmation before applying credit"

# Confirm the credit
RESPONSE=$(invoke_agent "$TENANT_A_TOKEN" "Yes, confirm the credit application")
assert_contains "$RESPONSE" "credit" "Response references the credit"

# ---------------------------------------------------------------------------
# Test 5: Tenant Isolation
# ---------------------------------------------------------------------------

log_test "Tenant Isolation — Tenant B cannot see Tenant A data"
RESPONSE=$(invoke_agent "$TENANT_B_TOKEN" "Show me my API usage summary for March 2025")
assert_not_contains "$RESPONSE" "tenant-alpha" "Tenant B response does not contain tenant-alpha data"
assert_contains "$RESPONSE" "api_calls" "Tenant B gets their own usage data"

# ---------------------------------------------------------------------------
# Test 6: Plan Catalog
# ---------------------------------------------------------------------------

log_test "Plan Catalog — Tenant Beta"
RESPONSE=$(invoke_agent "$TENANT_B_TOKEN" "What plans are available?")
assert_contains "$RESPONSE" "plan" "Response lists available plans"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "================================================================"
echo "TEST SUMMARY"
echo "================================================================"
echo "  Passed: $PASS_COUNT"
echo "  Failed: $FAIL_COUNT"
echo "  Total:  $((PASS_COUNT + FAIL_COUNT))"
echo "================================================================"

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "RESULT: SOME TESTS FAILED"
    exit 1
else
    echo "RESULT: ALL TESTS PASSED"
    exit 0
fi
