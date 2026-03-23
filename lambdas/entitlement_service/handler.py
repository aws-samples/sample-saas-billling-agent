"""EntitlementService Lambda handler.

Provides MCP tools for querying tenant plan entitlements, checking quota
usage against limits, browsing the plan catalog, and recommending plan
upgrades.  Queries against the Entitlements and PlanCatalog tables use
tenant-scoped partition keys.  The PlanCatalog scan is acceptable for a
small catalog table.

Tools:
    get_current_plan    — retrieve the active plan for a tenant
    check_quota         — compute usage vs. limits with threshold flag
    get_plan_catalog    — list all available plans
    recommend_upgrade   — suggest the optimal plan based on usage
"""

import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_ENTITLEMENTS_TABLE = os.environ.get("ENTITLEMENTS_TABLE", "Entitlements")
_PLAN_CATALOG_TABLE = os.environ.get("PLAN_CATALOG_TABLE", "PlanCatalog")
_USAGE_RECORDS_TABLE = os.environ.get("USAGE_RECORDS_TABLE", "UsageRecords")

_dynamodb = boto3.resource("dynamodb")
_entitlements_table = _dynamodb.Table(_ENTITLEMENTS_TABLE)
_plan_catalog_table = _dynamodb.Table(_PLAN_CATALOG_TABLE)
_usage_table = _dynamodb.Table(_USAGE_RECORDS_TABLE)

_APPROACHING_LIMIT_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decimal_to_number(obj):
    """Recursively convert Decimal values to int/float for JSON serialisation."""
    if isinstance(obj, list):
        return [_decimal_to_number(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _decimal_to_number(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        if obj == int(obj):
            return int(obj)
        return float(obj)
    return obj


def _get_current_month() -> str:
    """Return the current month as YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _get_current_usage(tenant_id: str, year_month: str) -> dict:
    """Query UsageRecords for *tenant_id* in *year_month* and aggregate totals.

    Returns ``api_calls`` and ``data_transfer_bytes`` summed across all
    endpoint records for the month.
    """
    pk = f"{tenant_id}#{year_month}"
    totals = {"api_calls": 0, "data_transfer_bytes": 0}

    params: dict = {
        "KeyConditionExpression": Key("pk").eq(pk),
    }

    while True:
        response = _usage_table.query(**params)
        for item in response.get("Items", []):
            totals["api_calls"] += int(item.get("api_calls", 0))
            totals["data_transfer_bytes"] += int(item.get("data_transfer_bytes", 0))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return totals


def _compute_quota_percentage(usage: int, limit: int) -> float:
    """Return usage as a percentage of the limit (0.0–100.0+).

    Returns 0.0 when the limit is zero or negative to avoid division errors.
    """
    if limit <= 0:
        return 0.0
    return (usage / limit) * 100.0


def _is_approaching_limit(usage: int, limit: int) -> bool:
    """Return True when usage exceeds 80 % of the limit."""
    if limit <= 0:
        return False
    return (usage / limit) > _APPROACHING_LIMIT_THRESHOLD


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_current_plan(tenant_id: str) -> dict:
    """Retrieve the active entitlement for *tenant_id*.

    Queries the Entitlements table with PK ``{tenant_id}`` and SK
    ``entitlement``.
    """
    response = _entitlements_table.get_item(
        Key={"pk": tenant_id, "sk": "entitlement"},
    )
    item = response.get("Item")
    if not item:
        return {"tenant_id": tenant_id, "error": "No active plan found."}
    return _decimal_to_number(item)


def check_quota(tenant_id: str) -> dict:
    """Compute current usage vs. plan limits for *tenant_id*.

    Returns usage percentages for API calls and data transfer, plus an
    ``is_approaching_limit`` flag that is ``True`` when *either* metric
    exceeds 80 % of its plan limit.
    """
    # 1. Get the tenant's current entitlement
    plan = get_current_plan(tenant_id)
    if "error" in plan:
        return plan

    api_call_limit = int(plan.get("api_call_limit", 0))
    data_transfer_limit_gb = float(plan.get("data_transfer_limit_gb", 0))
    data_transfer_limit_bytes = int(data_transfer_limit_gb * 1_073_741_824)  # GB → bytes

    # 2. Get current month usage from UsageRecords
    year_month = _get_current_month()
    usage = _get_current_usage(tenant_id, year_month)

    api_calls_used = usage["api_calls"]
    data_transfer_used = usage["data_transfer_bytes"]

    # 3. Compute percentages and threshold flag
    api_pct = _compute_quota_percentage(api_calls_used, api_call_limit)
    data_pct = _compute_quota_percentage(data_transfer_used, data_transfer_limit_bytes)

    approaching = (
        _is_approaching_limit(api_calls_used, api_call_limit)
        or _is_approaching_limit(data_transfer_used, data_transfer_limit_bytes)
    )

    return {
        "tenant_id": tenant_id,
        "plan_id": plan.get("plan_id"),
        "year_month": year_month,
        "api_calls": {
            "used": api_calls_used,
            "limit": api_call_limit,
            "percentage": round(api_pct, 2),
        },
        "data_transfer": {
            "used_bytes": data_transfer_used,
            "limit_bytes": data_transfer_limit_bytes,
            "percentage": round(data_pct, 2),
        },
        "is_approaching_limit": approaching,
    }


def get_plan_catalog() -> dict:
    """Return all available plans from the PlanCatalog table.

    Uses a Scan — acceptable for a small catalog table that is not
    tenant-scoped.
    """
    plans: list[dict] = []

    params: dict = {}
    while True:
        response = _plan_catalog_table.scan(**params)
        for item in response.get("Items", []):
            plans.append(_decimal_to_number(item))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return {"plans": plans}


def recommend_upgrade(tenant_id: str) -> dict:
    """Analyse usage patterns and suggest the optimal plan for *tenant_id*.

    The recommendation logic:
    1. Fetch the tenant's current plan and quota status.
    2. Fetch all available plans from the catalog.
    3. Filter plans that have higher limits than current usage.
    4. Among qualifying plans, pick the cheapest one that provides at
       least 20 % headroom above current usage.
    """
    quota = check_quota(tenant_id)
    if "error" in quota:
        return quota

    current_plan_id = quota.get("plan_id")
    api_used = quota["api_calls"]["used"]
    data_used = quota["data_transfer"]["used_bytes"]

    catalog = get_plan_catalog()
    plans = catalog.get("plans", [])

    if not plans:
        return {
            "tenant_id": tenant_id,
            "recommendation": None,
            "reason": "No plans available in catalog.",
        }

    # Find plans that give at least 20% headroom over current usage
    headroom_factor = 1.2
    candidates = []
    for plan in plans:
        plan_api_limit = int(plan.get("api_call_limit", 0))
        plan_data_limit_gb = float(plan.get("data_transfer_limit_gb", 0))
        plan_data_limit_bytes = int(plan_data_limit_gb * 1_073_741_824)

        if (
            plan_api_limit >= api_used * headroom_factor
            and plan_data_limit_bytes >= data_used * headroom_factor
        ):
            candidates.append(plan)

    if not candidates:
        return {
            "tenant_id": tenant_id,
            "current_plan_id": current_plan_id,
            "recommendation": None,
            "reason": "No plan in the catalog provides sufficient headroom for current usage.",
        }

    # Sort by price and pick the cheapest qualifying plan
    candidates.sort(key=lambda p: int(p.get("price_cents_monthly", 0)))
    best = candidates[0]

    # Don't recommend the same plan
    if best.get("pk") == current_plan_id:
        return {
            "tenant_id": tenant_id,
            "current_plan_id": current_plan_id,
            "recommendation": None,
            "reason": "Current plan is already optimal for your usage.",
        }

    return {
        "tenant_id": tenant_id,
        "current_plan_id": current_plan_id,
        "recommendation": {
            "plan_id": best.get("pk"),
            "name": best.get("name"),
            "price_cents_monthly": int(best.get("price_cents_monthly", 0)),
            "api_call_limit": int(best.get("api_call_limit", 0)),
            "data_transfer_limit_gb": float(best.get("data_transfer_limit_gb", 0)),
        },
        "reason": "This plan provides sufficient headroom at the lowest cost.",
    }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    """Route incoming MCP Gateway events to the appropriate tool function.

    Expected event shape::

        {
            "tool_name": "get_current_plan",
            "parameters": {
                "tenant_id": "tenant-abc"
            }
        }
    """
    tool_name = event.get("tool_name") or event.get("__tool_name__") or event.get("name")
    params = event.get("parameters", event.get("arguments", {}))

    # Gateway sends flat args — use discriminator fields to identify the tool
    valid_tools = ("get_current_plan", "check_quota", "get_plan_catalog", "recommend_upgrade")
    if not tool_name or tool_name not in valid_tools:
        params = event if "tenant_id" in event else params
        if params.get("catalog"):
            tool_name = "get_plan_catalog"
        elif params.get("check_quota"):
            tool_name = "check_quota"
        elif params.get("recommend"):
            tool_name = "recommend_upgrade"
        elif params.get("plan_info"):
            tool_name = "get_current_plan"
        elif not params.get("tenant_id"):
            tool_name = "get_plan_catalog"
        else:
            tool_name = "get_current_plan"

    try:
        if tool_name == "get_plan_catalog":
            # get_plan_catalog does not require tenant_id
            return get_plan_catalog()

        tenant_id = params.get("tenant_id")
        if not tenant_id:
            return {"error": "Missing required parameter: tenant_id", "status": 400}

        if tool_name == "get_current_plan":
            return get_current_plan(tenant_id)

        elif tool_name == "check_quota":
            return check_quota(tenant_id)

        elif tool_name == "recommend_upgrade":
            return recommend_upgrade(tenant_id)

        else:
            return {"error": f"Unknown tool: {tool_name}", "status": 400}

    except ValueError as exc:
        return {"error": str(exc), "status": 400}
