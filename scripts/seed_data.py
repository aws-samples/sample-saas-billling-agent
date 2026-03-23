#!/usr/bin/env python3
"""Seed DynamoDB tables with sample multi-tenant data.

Populates all four tables (UsageRecords, BillingRecords, Entitlements,
PlanCatalog) with realistic data for 2 tenants spanning 2025-2026.

Uses ``put_item`` with conditional writes (``attribute_not_exists``) for
idempotency — re-running the script will not overwrite existing records.
"""

import os
import random
import sys

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USAGE_RECORDS_TABLE = os.environ.get("USAGE_RECORDS_TABLE", "UsageRecords")
BILLING_RECORDS_TABLE = os.environ.get("BILLING_RECORDS_TABLE", "BillingRecords")
ENTITLEMENTS_TABLE = os.environ.get("ENTITLEMENTS_TABLE", "Entitlements")
PLAN_CATALOG_TABLE = os.environ.get("PLAN_CATALOG_TABLE", "PlanCatalog")

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

TENANTS = ["tenant-alpha", "tenant-beta"]
MONTHS = ["2025-12", "2026-01", "2026-02", "2026-03"]
ENDPOINTS = ["/api/users", "/api/orders", "/api/analytics", "/api/reports", "/api/webhooks"]

PLANS = [
    {
        "pk": "plan-starter",
        "sk": "v1",
        "name": "Starter",
        "price_cents_monthly": 2900,
        "api_call_limit": 10000,
        "data_transfer_limit_gb": 10,
        "features": ["basic-analytics", "email-support"],
    },
    {
        "pk": "plan-pro",
        "sk": "v1",
        "name": "Pro",
        "price_cents_monthly": 9900,
        "api_call_limit": 100000,
        "data_transfer_limit_gb": 100,
        "features": ["advanced-analytics", "priority-support", "custom-reports"],
    },
    {
        "pk": "plan-enterprise",
        "sk": "v1",
        "name": "Enterprise",
        "price_cents_monthly": 29900,
        "api_call_limit": 1000000,
        "data_transfer_limit_gb": 1000,
        "features": ["advanced-analytics", "dedicated-support", "custom-reports", "sla-guarantee", "sso"],
    },
]

ENTITLEMENTS = [
    {
        "pk": "tenant-alpha",
        "sk": "entitlement",
        "plan_id": "plan-pro",
        "api_call_limit": 100000,
        "data_transfer_limit_gb": 100,
        "started_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    },
    {
        "pk": "tenant-beta",
        "sk": "entitlement",
        "plan_id": "plan-starter",
        "api_call_limit": 10000,
        "data_transfer_limit_gb": 10,
        "started_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_dynamodb_resource():
    return boto3.resource("dynamodb", region_name=REGION)


def _conditional_put(table, item):
    """Put an item only if it does not already exist (idempotent)."""
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _generate_usage_records(tenant_id, months, endpoints):
    records = []
    is_alpha = tenant_id == "tenant-alpha"
    for month in months:
        for endpoint in endpoints:
            num_records = random.randint(3, 5)
            for day in sorted(random.sample(range(1, 29), min(num_records, 28))):
                for hour in random.sample(range(0, 24), random.randint(1, 2)):
                    timestamp = f"{month}-{day:02d}T{hour:02d}:00:00Z"
                    records.append({
                        "pk": f"{tenant_id}#{month}",
                        "sk": f"{endpoint}#{timestamp}",
                        "api_calls": random.randint(500, 8000) if is_alpha else random.randint(100, 2000),
                        "data_transfer_bytes": random.randint(100000, 10000000) if is_alpha else random.randint(10000, 2000000),
                        "compute_seconds": random.randint(20, 800) if is_alpha else random.randint(5, 200),
                        "endpoint_name": endpoint,
                    })
    return records


def _generate_billing_records(tenant_id, months):
    records = []
    is_alpha = tenant_id == "tenant-alpha"

    for i, month in enumerate(months):
        status = "sent" if i < len(months) - 1 else "draft"
        amount = random.randint(8000, 45000) if is_alpha else random.randint(2000, 12000)
        records.append({
            "pk": tenant_id,
            "sk": f"invoice#{month}",
            "amount_cents": amount,
            "status": status,
            "line_items": [
                {"description": "API calls", "amount_cents": int(amount * 0.5)},
                {"description": "Data transfer", "amount_cents": int(amount * 0.3)},
                {"description": "Compute", "amount_cents": int(amount * 0.2)},
            ],
            "credits_applied": 0,
            "due_date": f"{month}-28T00:00:00Z",
        })

    # Balance record
    records.append({
        "pk": tenant_id,
        "sk": "balance",
        "balance_cents": random.randint(5000, 35000) if is_alpha else random.randint(1000, 8000),
    })

    # Credit record
    records.append({
        "pk": tenant_id,
        "sk": "credit#2026-02-10T14:30:00Z",
        "amount_cents": 3000,
        "reason": "Service disruption compensation",
    })

    return records


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------

def seed_plan_catalog(dynamodb):
    table = dynamodb.Table(PLAN_CATALOG_TABLE)
    written = sum(1 for plan in PLANS if _conditional_put(table, plan))
    print(f"  PlanCatalog: {written} new, {len(PLANS) - written} existed")
    return written


def seed_entitlements(dynamodb):
    table = dynamodb.Table(ENTITLEMENTS_TABLE)
    # Use unconditional put so entitlements are always up to date
    for ent in ENTITLEMENTS:
        table.put_item(Item=ent)
    print(f"  Entitlements: {len(ENTITLEMENTS)} written")
    return len(ENTITLEMENTS)


def seed_usage_records(dynamodb):
    table = dynamodb.Table(USAGE_RECORDS_TABLE)
    written = skipped = 0
    for tenant_id in TENANTS:
        for record in _generate_usage_records(tenant_id, MONTHS, ENDPOINTS):
            if _conditional_put(table, record):
                written += 1
            else:
                skipped += 1
    print(f"  UsageRecords: {written} new, {skipped} existed")
    return written


def seed_billing_records(dynamodb):
    table = dynamodb.Table(BILLING_RECORDS_TABLE)
    written = skipped = 0
    for tenant_id in TENANTS:
        for record in _generate_billing_records(tenant_id, MONTHS):
            if _conditional_put(table, record):
                written += 1
            else:
                skipped += 1
    print(f"  BillingRecords: {written} new, {skipped} existed")
    return written


def seed_all():
    dynamodb = _get_dynamodb_resource()
    print("Seeding DynamoDB tables...")
    seed_plan_catalog(dynamodb)
    seed_entitlements(dynamodb)
    seed_usage_records(dynamodb)
    seed_billing_records(dynamodb)
    print("\nSeed data complete.")


# ---------------------------------------------------------------------------
# Public API for testing
# ---------------------------------------------------------------------------

def get_seed_config():
    return {
        "tenants": TENANTS,
        "months": MONTHS,
        "endpoints": ENDPOINTS,
        "plans": PLANS,
        "entitlements": ENTITLEMENTS,
    }


def main():
    try:
        seed_all()
        return 0
    except Exception as exc:
        print(f"\nError seeding data: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    random.seed(42)
    sys.exit(main())
