"""UsageService Lambda handler.

Provides MCP tools for querying tenant API usage data from the
UsageRecords DynamoDB table. All queries are scoped by tenant_id
in the partition key — no Scan operations are used.

Tools:
    get_usage_summary — aggregate usage for a tenant in a given month
    get_usage_by_endpoint — usage breakdown for a specific endpoint
    get_usage_trend — monthly usage array across a date range
"""

import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_TABLE_NAME = os.environ.get("USAGE_RECORDS_TABLE", "UsageRecords")
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(_TABLE_NAME)

_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_year_month(value: str) -> None:
    """Raise ValueError when *value* is not in YYYY-MM format."""
    if not isinstance(value, str) or not _YEAR_MONTH_RE.match(value):
        raise ValueError(f"Invalid year_month format: '{value}'. Expected YYYY-MM.")


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


def _next_month(year_month: str) -> str:
    """Return the YYYY-MM string for the month after *year_month*."""
    year, month = int(year_month[:4]), int(year_month[5:7])
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def _month_range(start_month: str, end_month: str) -> list[str]:
    """Return an inclusive list of YYYY-MM strings from *start* to *end*."""
    months: list[str] = []
    current = start_month
    while current <= end_month:
        months.append(current)
        current = _next_month(current)
    return months


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_usage_summary(tenant_id: str, year_month: str) -> dict:
    """Aggregate usage for *tenant_id* in *year_month*.

    Returns totals for ``api_calls``, ``data_transfer_bytes``, and
    ``compute_seconds``.
    """
    _validate_year_month(year_month)

    pk = f"{tenant_id}#{year_month}"
    totals = {"api_calls": 0, "data_transfer_bytes": 0, "compute_seconds": 0}

    params: dict = {
        "KeyConditionExpression": Key("pk").eq(pk),
    }

    while True:
        response = _table.query(**params)
        for item in response.get("Items", []):
            totals["api_calls"] += int(item.get("api_calls", 0))
            totals["data_transfer_bytes"] += int(item.get("data_transfer_bytes", 0))
            totals["compute_seconds"] += int(item.get("compute_seconds", 0))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return {
        "tenant_id": tenant_id,
        "year_month": year_month,
        "api_calls": totals["api_calls"],
        "data_transfer_bytes": totals["data_transfer_bytes"],
        "compute_seconds": totals["compute_seconds"],
    }


def get_usage_by_endpoint(tenant_id: str, endpoint: str, year_month: str) -> dict:
    """Return usage for a specific *endpoint* within *year_month*.

    Queries with ``begins_with`` on the sort key so only records for the
    requested endpoint are returned.
    """
    _validate_year_month(year_month)

    pk = f"{tenant_id}#{year_month}"
    totals = {"api_calls": 0, "data_transfer_bytes": 0, "compute_seconds": 0}
    records: list[dict] = []

    params: dict = {
        "KeyConditionExpression": Key("pk").eq(pk) & Key("sk").begins_with(f"{endpoint}#"),
    }

    while True:
        response = _table.query(**params)
        for item in response.get("Items", []):
            totals["api_calls"] += int(item.get("api_calls", 0))
            totals["data_transfer_bytes"] += int(item.get("data_transfer_bytes", 0))
            totals["compute_seconds"] += int(item.get("compute_seconds", 0))
            records.append(_decimal_to_number(item))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return {
        "tenant_id": tenant_id,
        "endpoint": endpoint,
        "year_month": year_month,
        "api_calls": totals["api_calls"],
        "data_transfer_bytes": totals["data_transfer_bytes"],
        "compute_seconds": totals["compute_seconds"],
        "records": records,
    }


def get_usage_trend(tenant_id: str, start_month: str, end_month: str) -> dict:
    """Return monthly usage totals from *start_month* to *end_month* inclusive.

    Each month is queried independently (no Scan). The result is an ordered
    array of monthly summaries suitable for trend analysis.
    """
    _validate_year_month(start_month)
    _validate_year_month(end_month)

    if start_month > end_month:
        raise ValueError(
            f"start_month ({start_month}) must not be after end_month ({end_month})."
        )

    months = _month_range(start_month, end_month)
    trend: list[dict] = []

    for month in months:
        summary = get_usage_summary(tenant_id, month)
        trend.append(summary)

    return {
        "tenant_id": tenant_id,
        "start_month": start_month,
        "end_month": end_month,
        "months": trend,
    }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    """Route incoming MCP Gateway events to the appropriate tool function.

    The Gateway sends just the tool arguments (no tool_name wrapper).
    We infer the tool from the arguments present.
    """

    tool_name = event.get("tool_name") or event.get("__tool_name__") or event.get("name")
    params = event.get("parameters", event.get("arguments", {}))

    # Gateway sends flat args — use discriminator fields to identify the tool
    if not tool_name or tool_name not in ("get_usage_summary", "get_usage_by_endpoint", "get_usage_trend"):
        params = event if "tenant_id" in event else params
        if "start_month" in params and "end_month" in params:
            tool_name = "get_usage_trend"
        elif "endpoint" in params:
            tool_name = "get_usage_by_endpoint"
        else:
            tool_name = "get_usage_summary"

    try:
        tenant_id = params["tenant_id"]
    except KeyError:
        return {"error": "Missing required parameter: tenant_id", "status": 400}

    try:
        if tool_name == "get_usage_summary":
            year_month = params.get("year_month")
            if not year_month:
                return {"error": "Missing required parameter: year_month", "status": 400}
            return get_usage_summary(tenant_id, year_month)

        elif tool_name == "get_usage_by_endpoint":
            endpoint = params.get("endpoint")
            year_month = params.get("year_month")
            if not endpoint or not year_month:
                return {
                    "error": "Missing required parameter: endpoint and year_month are required",
                    "status": 400,
                }
            return get_usage_by_endpoint(tenant_id, endpoint, year_month)

        elif tool_name == "get_usage_trend":
            start_month = params.get("start_month")
            end_month = params.get("end_month")
            if not start_month or not end_month:
                return {
                    "error": "Missing required parameter: start_month and end_month are required",
                    "status": 400,
                }
            return get_usage_trend(tenant_id, start_month, end_month)

        else:
            return {"error": f"Unknown tool: {tool_name}", "status": 400}

    except ValueError as exc:
        return {"error": str(exc), "status": 400}
