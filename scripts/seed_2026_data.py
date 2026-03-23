"""Seed 2026 data for testing quick actions."""
import os
import random
import sys

import boto3

dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
usage_tbl = dynamodb.Table(os.environ["USAGE_RECORDS_TABLE"])
billing_tbl = dynamodb.Table(os.environ["BILLING_RECORDS_TABLE"])
ent_tbl = dynamodb.Table(os.environ["ENTITLEMENTS_TABLE"])

TENANTS = ["tenant-alpha", "tenant-beta"]
MONTHS = ["2026-01", "2026-02", "2026-03"]
ENDPOINTS = ["/api/users", "/api/orders", "/api/analytics", "/api/reports", "/api/webhooks"]
random.seed(2026)

# Usage records
ct = 0
for t in TENANTS:
    for m in MONTHS:
        for ep in ENDPOINTS:
            for d in sorted(random.sample(range(1, 29), random.randint(3, 5))):
                for h in random.sample(range(0, 24), random.randint(1, 2)):
                    ts = f"{m}-{d:02d}T{h:02d}:00:00Z"
                    calls = random.randint(500, 8000) if t == "tenant-alpha" else random.randint(100, 2000)
                    data_b = random.randint(100000, 10000000) if t == "tenant-alpha" else random.randint(10000, 2000000)
                    comp = random.randint(20, 800) if t == "tenant-alpha" else random.randint(5, 200)
                    try:
                        usage_tbl.put_item(
                            Item={"pk": f"{t}#{m}", "sk": f"{ep}#{ts}",
                                  "api_calls": calls, "data_transfer_bytes": data_b,
                                  "compute_seconds": comp, "endpoint_name": ep},
                            ConditionExpression="attribute_not_exists(pk)")
                        ct += 1
                    except Exception:
                        pass
print(f"Usage: {ct} new records")

# Invoices + credits + balances
ic = 0
for t in TENANTS:
    for i, m in enumerate(MONTHS):
        amt = random.randint(8000, 45000) if t == "tenant-alpha" else random.randint(2000, 12000)
        st = "sent" if i < 2 else "draft"
        try:
            billing_tbl.put_item(
                Item={"pk": t, "sk": f"invoice#{m}",
                      "amount_cents": amt, "status": st,
                      "line_items": [{"description": "API calls", "amount_cents": int(amt * 0.5)},
                                     {"description": "Data transfer", "amount_cents": int(amt * 0.3)},
                                     {"description": "Compute", "amount_cents": int(amt * 0.2)}],
                      "credits_applied": 0, "due_date": f"{m}-28T00:00:00Z"},
                ConditionExpression="attribute_not_exists(pk)")
            ic += 1
        except Exception:
            pass
    try:
        billing_tbl.put_item(
            Item={"pk": t, "sk": "credit#2026-02-10T14:30:00Z",
                  "amount_cents": 3000, "reason": "Service disruption compensation"},
            ConditionExpression="attribute_not_exists(pk)")
        ic += 1
    except Exception:
        pass
    billing_tbl.put_item(
        Item={"pk": t, "sk": "balance",
              "balance_cents": random.randint(5000, 35000) if t == "tenant-alpha" else random.randint(1000, 8000)})
print(f"Billing: {ic} new records")

# Entitlements (2026 dates)
for t, plan, api_lim, data_gb in [("tenant-alpha", "plan-pro", 100000, 100),
                                    ("tenant-beta", "plan-starter", 10000, 10)]:
    ent_tbl.put_item(
        Item={"pk": t, "sk": "entitlement", "plan_id": plan,
              "api_call_limit": api_lim, "data_transfer_limit_gb": data_gb,
              "started_at": "2026-01-01T00:00:00Z", "expires_at": "2027-01-01T00:00:00Z"})
print("Entitlements: updated for 2026")
print("\nDone! 2026 data ready for testing.")
