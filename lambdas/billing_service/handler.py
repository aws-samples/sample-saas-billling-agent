"""BillingService Lambda handler.

Provides MCP tools for invoice generation, invoice history, credit
application, balance queries, and invoice confirmation against the
BillingRecords DynamoDB table.  All queries are scoped by tenant_id
in the partition key — no Scan operations are used.

Tools:
    generate_invoice  — create a draft invoice for a given month
    get_invoice_history — list past invoices for a tenant
    apply_credit      — record a credit and update the tenant balance
    get_balance       — retrieve (or initialise) the tenant balance
    confirm_invoice   — transition an invoice from draft to sent
"""

import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_TABLE_NAME = os.environ.get("BILLING_RECORDS_TABLE", "BillingRecords")
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


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def generate_invoice(tenant_id: str, year_month: str) -> dict:
    """Create a draft invoice for *tenant_id* in *year_month*.

    If an invoice already exists for the given month the existing record is
    returned instead of creating a duplicate.  New invoices are always
    created with status ``draft``.
    """
    _validate_year_month(year_month)

    sk = f"invoice#{year_month}"

    # Check for existing invoice
    existing = _table.get_item(Key={"pk": tenant_id, "sk": sk}).get("Item")
    if existing:
        return _decimal_to_number(existing)

    # Compute a simple due date — 30 days from now
    due_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    item = {
        "pk": tenant_id,
        "sk": sk,
        "amount_cents": 0,
        "status": "draft",
        "line_items": [],
        "credits_applied": 0,
        "due_date": due_date,
    }

    _table.put_item(Item=item)

    return _decimal_to_number(item)


def get_invoice_history(tenant_id: str) -> dict:
    """Return all invoices for *tenant_id*, ordered by sort key.

    Queries with ``begins_with('invoice#')`` on the sort key so only
    invoice records are returned.
    """
    invoices: list[dict] = []

    params: dict = {
        "KeyConditionExpression": Key("pk").eq(tenant_id) & Key("sk").begins_with("invoice#"),
    }

    while True:
        response = _table.query(**params)
        for item in response.get("Items", []):
            invoices.append(_decimal_to_number(item))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return {
        "tenant_id": tenant_id,
        "invoices": invoices,
    }


def apply_credit(tenant_id: str, amount_cents: int, reason: str) -> dict:
    """Record a credit for *tenant_id* and update the balance.

    *amount_cents* must be a positive integer.  Negative values are
    rejected with a ``ValueError``.
    """
    amount_cents = int(amount_cents)
    if amount_cents <= 0:
        raise ValueError("Credit amount_cents must be a positive integer.")

    # 1. Write the credit record
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    credit_sk = f"credit#{timestamp}"

    credit_item = {
        "pk": tenant_id,
        "sk": credit_sk,
        "amount_cents": amount_cents,
        "reason": reason,
    }
    _table.put_item(Item=credit_item)

    # 2. Update (or create) the balance record atomically
    balance_response = _table.update_item(
        Key={"pk": tenant_id, "sk": "balance"},
        UpdateExpression="ADD balance_cents :amt",
        ExpressionAttributeValues={":amt": -amount_cents},
        ReturnValues="ALL_NEW",
    )

    updated_balance = _decimal_to_number(balance_response.get("Attributes", {}))

    return {
        "tenant_id": tenant_id,
        "credit_applied": amount_cents,
        "reason": reason,
        "updated_balance": updated_balance,
    }


def get_balance(tenant_id: str) -> dict:
    """Return the current balance for *tenant_id*.

    If no balance record exists a zero-balance record is created and
    returned.
    """
    response = _table.get_item(Key={"pk": tenant_id, "sk": "balance"})
    item = response.get("Item")

    if item:
        return _decimal_to_number(item)

    # Create zero-balance record
    zero_balance = {
        "pk": tenant_id,
        "sk": "balance",
        "balance_cents": 0,
    }
    _table.put_item(Item=zero_balance)
    return _decimal_to_number(zero_balance)


def confirm_invoice(tenant_id: str, year_month: str) -> dict:
    """Transition an invoice from ``draft`` to ``sent``.

    Only invoices with status ``draft`` can be confirmed.  Returns the
    updated invoice record.
    """
    _validate_year_month(year_month)

    sk = f"invoice#{year_month}"

    try:
        response = _table.update_item(
            Key={"pk": tenant_id, "sk": sk},
            UpdateExpression="SET #s = :new_status",
            ConditionExpression="#s = :draft_status",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new_status": "sent",
                ":draft_status": "draft",
            },
            ReturnValues="ALL_NEW",
        )
        return _decimal_to_number(response.get("Attributes", {}))
    except _dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Either the invoice doesn't exist or it's not in draft status
        existing = _table.get_item(Key={"pk": tenant_id, "sk": sk}).get("Item")
        if not existing:
            raise ValueError(f"No invoice found for {year_month}.")
        raise ValueError(
            f"Invoice for {year_month} has status '{existing.get('status')}' and cannot be confirmed."
        )


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    """Route incoming MCP Gateway events to the appropriate tool function.

    Expected event shape::

        {
            "tool_name": "generate_invoice",
            "parameters": {
                "tenant_id": "tenant-abc",
                "year_month": "2025-03"
            }
        }
    """
    tool_name = event.get("tool_name") or event.get("__tool_name__") or event.get("name")
    params = event.get("parameters", event.get("arguments", {}))

    # Gateway sends flat args — use discriminator fields to identify the tool
    valid_tools = ("generate_invoice", "get_invoice_history", "apply_credit", "get_balance", "confirm_invoice")
    if not tool_name or tool_name not in valid_tools:
        params = event if "tenant_id" in event else params
        if "amount_cents" in params:
            tool_name = "apply_credit"
        elif params.get("confirm") and "year_month" in params:
            tool_name = "confirm_invoice"
        elif params.get("history"):
            tool_name = "get_invoice_history"
        elif params.get("balance"):
            tool_name = "get_balance"
        elif "year_month" in params:
            tool_name = "generate_invoice"
        else:
            tool_name = "get_balance"

    try:
        tenant_id = params["tenant_id"]
    except KeyError:
        return {"error": "Missing required parameter: tenant_id", "status": 400}

    try:
        if tool_name == "generate_invoice":
            year_month = params.get("year_month")
            if not year_month:
                return {"error": "Missing required parameter: year_month", "status": 400}
            return generate_invoice(tenant_id, year_month)

        elif tool_name == "get_invoice_history":
            return get_invoice_history(tenant_id)

        elif tool_name == "apply_credit":
            amount_cents = params.get("amount_cents")
            reason = params.get("reason")
            if amount_cents is None:
                return {"error": "Missing required parameter: amount_cents", "status": 400}
            if not reason:
                return {"error": "Missing required parameter: reason", "status": 400}
            return apply_credit(tenant_id, amount_cents, reason)

        elif tool_name == "get_balance":
            return get_balance(tenant_id)

        elif tool_name == "confirm_invoice":
            year_month = params.get("year_month")
            if not year_month:
                return {"error": "Missing required parameter: year_month", "status": 400}
            return confirm_invoice(tenant_id, year_month)

        else:
            return {"error": f"Unknown tool: {tool_name}", "status": 400}

    except ValueError as exc:
        return {"error": str(exc), "status": 400}
