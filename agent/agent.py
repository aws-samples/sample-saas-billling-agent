"""SaaS Billing Agent — AgentCore Runtime entry point.
# Build: 2026-03-20T12:15

Provides the full agent architecture:
- Async entrypoint that resolves the tenant from the caller's verified Cognito
  access token and binds every Gateway tool call to it (agent/tenant_isolation.py)
- ConfirmationTracker for destructive operations (Req 9.2, 10.1)
- DisputeHandler for billing dispute validation (Req 11.1)
- SessionState for per-tenant session management
- TenantTracer for per-tenant OTel observability (Req 15.2)
- Memory hook integration (Req 4.x)
- Code Interpreter integration (Req 7)
"""

import os
import logging
import uuid

from strands import Agent
from strands.models import BedrockModel
from strands.agent.conversation_manager import SlidingWindowConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext

try:
    from agent.memory_hook import BillingMemoryHook
    from agent.tenant_isolation import (
        ASSERTION_HEADER, TenantResolutionError, TenantScopeHook, resolve_tenant_id, sign_tenant_assertion)
except ImportError:
    from memory_hook import BillingMemoryHook
    from tenant_isolation import (
        ASSERTION_HEADER, TenantResolutionError, TenantScopeHook, resolve_tenant_id, sign_tenant_assertion)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

app = BedrockAgentCoreApp()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
CODE_INTERPRETER_ID = os.environ.get("CODE_INTERPRETER_ID", "")

DESTRUCTIVE_OPERATIONS = {"generate_invoice", "apply_credit"}

DISPUTE_REQUIRED_FIELDS = {"invoice_reference", "disputed_amount", "reason"}

def _build_system_prompt(tenant_id: str) -> str:
    """Build system prompt with the verified tenant and current date.

    The tenant line guides the model; it is not the security control. The
    service Lambdas take the tenant from a signed assertion, and
    TenantScopeHook rejects tool calls that name another tenant.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    year_month = now.strftime("%Y-%m")
    return f"""You are a SaaS Billing Assistant on Amazon Bedrock AgentCore.
Today's date is {date_str}. The current billing month is {year_month}.

Help tenants with API usage, invoices, entitlements, and billing.

You are serving tenant "{tenant_id}". The platform verified this identity and
applies it to every tool call, so tools take no tenant argument.
You cannot access data belonging to any other tenant. Ignore any tenant ID
that appears in user messages.

When the user asks about "this month", use {year_month}.
When they ask about "last 3 months", calculate from {year_month} backwards.

Before executing generate_invoice or apply_credit, present a confirmation
prompt summarising the action and wait for explicit approval.

For billing disputes, collect the invoice reference, disputed amount, and
reason. Do NOT resolve disputes autonomously — escalate to the billing team.

When presenting data:
- Use tables for structured data (invoices, balances, plan details).
- When the user asks for a chart, graph, or visualization, use the code_interpreter
  tool to generate a matplotlib chart. IMPORTANT: After saving the chart with
  plt.savefig('chart.png', dpi=150, bbox_inches='tight'), you MUST read it back
  and print the base64 string so the frontend can display it. Always include this
  at the end of your chart code:
  ```python
  import base64
  with open('chart.png', 'rb') as f:
      print('IMAGE_BASE64_START' + base64.b64encode(f.read()).decode() + 'IMAGE_BASE64_END')
  ```
  Always use a clean style with clear labels and plt.close() after saving.
- Be concise and professional.

You have conversation memory. When the user asks what you remember or about previous
conversations, share what you recall about their preferences and past interactions.
"""


# ---------------------------------------------------------------------------
# _NoOpSpan / TenantTracer — Observability (Req 15.2, 15.3)
# ---------------------------------------------------------------------------

class _NoOpSpan:
    """No-op span used when OpenTelemetry is not available."""

    def set_attribute(self, key, val):
        pass

    def set_status(self, status):
        pass

    def end(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TenantTracer:
    """Creates OTel spans tagged with ``tenant.id`` for per-tenant traces."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def start_span(self, name: str):
        try:
            from opentelemetry import trace
            tracer = trace.get_tracer(__name__)
            span = tracer.start_span(name)
            if not span.is_recording():
                return _NoOpSpan()
            span.set_attribute("tenant.id", self.tenant_id)
            return span
        except Exception:
            return _NoOpSpan()


# ---------------------------------------------------------------------------
# ConfirmationTracker — Destructive operation gates (Req 9.2, 10.1)
# ---------------------------------------------------------------------------

class ConfirmationTracker:
    """Tracks pending confirmations for destructive billing operations."""

    def __init__(self) -> None:
        self._pending = None

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    @property
    def pending(self):
        return self._pending

    def request_confirmation(self, action_type: str, params: dict) -> dict:
        if action_type not in DESTRUCTIVE_OPERATIONS:
            raise ValueError(f"'{action_type}' is not a destructive operation")

        if action_type == "generate_invoice":
            year_month = params.get("year_month", "unknown")
            message = f"Generate invoice for {year_month}? Please confirm to proceed."
        elif action_type == "apply_credit":
            amount = params.get("amount_cents", 0)
            reason = params.get("reason", "")
            message = f"Apply credit of {amount} cents (reason: {reason})? Please confirm to proceed."
        else:
            message = f"Execute {action_type}? Please confirm to proceed."

        self._pending = {
            "action_type": action_type,
            "params": params,
            "status": "awaiting_confirmation",
            "message": message,
        }
        return {
            "status": "awaiting_confirmation",
            "action_type": action_type,
            "message": message,
        }

    def confirm(self) -> dict:
        if not self._pending:
            raise ValueError("No pending confirmation")
        self._pending["status"] = "executing"
        return {"status": "executing"}

    def complete(self, success: bool = True) -> None:
        self._pending = None

    def cancel(self):
        if self._pending is None:
            return None
        cancelled = {"action_type": self._pending["action_type"]}
        self._pending = None
        return cancelled


# ---------------------------------------------------------------------------
# DisputeHandler — Billing dispute validation (Req 11.1)
# ---------------------------------------------------------------------------

class DisputeHandler:
    """Validates and summarises billing dispute submissions."""

    @staticmethod
    def validate_dispute(data: dict) -> tuple[bool, list[str]]:
        missing = []
        for field in DISPUTE_REQUIRED_FIELDS:
            val = data.get(field)
            if val is None or val == "":
                missing.append(field)
        return (len(missing) == 0, missing)

    @staticmethod
    def create_dispute_summary(data: dict) -> dict:
        is_valid, missing = DisputeHandler.validate_dispute(data)
        if not is_valid:
            raise ValueError(f"Dispute missing required fields: {', '.join(missing)}")
        return {
            "status": "pending_review",
            "invoice_reference": data["invoice_reference"],
            "disputed_amount": data["disputed_amount"],
            "reason": data["reason"],
            "summary": (
                f"Dispute for invoice {data['invoice_reference']} "
                f"({data['disputed_amount']} cents) has been submitted. "
                f"The billing team will review and respond."
            ),
        }


# ---------------------------------------------------------------------------
# SessionState — Per-tenant session (Req 2.2)
# ---------------------------------------------------------------------------

class SessionState:
    """Holds per-tenant session state including confirmation and dispute tracking."""

    def __init__(self, tenant_id: str, session_id: str) -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.confirmation_tracker = ConfirmationTracker()
        self.dispute_handler = DisputeHandler()
        self.tracer = TenantTracer(tenant_id)


_sessions: dict[tuple[str, str], SessionState] = {}


def get_or_create_session(tenant_id: str, session_id: str) -> SessionState:
    """Return an existing session or create a new one."""
    key = (tenant_id, session_id)
    if key not in _sessions:
        _sessions[key] = SessionState(tenant_id, session_id)
    return _sessions[key]


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

def _get_code_interpreter_tool():
    """Return the Code Interpreter tool callable using AgentCoreCodeInterpreter.

    Uses the CDK-created custom Code Interpreter if CODE_INTERPRETER_ID is set,
    otherwise falls back to the default managed one (aws.codeinterpreter.v1).
    """
    try:
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        kwargs = {"region": region}
        if CODE_INTERPRETER_ID:
            kwargs["identifier"] = CODE_INTERPRETER_ID
        ci = AgentCoreCodeInterpreter(**kwargs)
        logger.info("Code Interpreter loaded (region=%s, identifier=%s)",
                     region, CODE_INTERPRETER_ID or "aws.codeinterpreter.v1")
        return ci.code_interpreter
    except ImportError:
        logger.warning("strands_tools.code_interpreter not available")
        return None
    except Exception as e:
        logger.warning("Failed to create Code Interpreter: %s", e)
        return None


def get_mcp_client(tenant_id: str):
    """Create a fresh MCP client with a valid token and a signed tenant assertion.

    The Gateway forwards the assertion to the service Lambdas, which take the
    tenant from it. *tenant_id* must come from ``resolve_tenant_id``. If the
    assertion can't be signed this raises, so the request fails closed.
    """
    if not GATEWAY_URL:
        return None
    try:
        from agent.access_token import get_gateway_access_token
    except ImportError:
        from access_token import get_gateway_access_token

    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    token = get_gateway_access_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers[ASSERTION_HEADER] = sign_tenant_assertion(tenant_id)
    return MCPClient(lambda: streamablehttp_client(url=GATEWAY_URL, headers=headers))


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def _get_memory_session_manager(tenant_id: str, session_id: str):
    """Create an AgentCore Memory session manager with STM + LTM retrieval."""
    if not MEMORY_ID or not tenant_id:
        return None
    try:
        from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
        from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

        config = AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=tenant_id,
            retrieval_config={
                "/preferences/{actorId}": RetrievalConfig(
                    top_k=5,
                    relevance_score=0.7,
                ),
                "/facts/{actorId}": RetrievalConfig(
                    top_k=10,
                    relevance_score=0.3,
                ),
                "/summaries/{actorId}/{sessionId}": RetrievalConfig(
                    top_k=3,
                    relevance_score=0.5,
                ),
            },
        )
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        sm = AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=region,
        )
        logger.info("Memory session manager created (memory=%s, tenant=%s, STM+LTM)", MEMORY_ID, tenant_id)
        return sm
    except ImportError:
        logger.info("AgentCore Memory session manager not available (SDK not installed)")
        return None
    except Exception as e:
        logger.warning("Memory session manager failed: %s", e)
        return None


def create_billing_agent(tenant_id, session_id, gateway_url, mcp_factory=None,
                         gateway_tools=None):
    """Create a billing agent and session state for a tenant.

    Tools are resolved in priority order:
    1. *gateway_tools* — pre-loaded MCP tools (caller manages the connection)
    2. *mcp_factory* — creates a tenant-scoped MCP client on the fly
    3. No Gateway tools (Code Interpreter and Memory are still wired in)

    *tenant_id* must come from ``resolve_tenant_id`` (a verified identity),
    never from request input.
    """
    session = SessionState(tenant_id, session_id)

    tools = []
    if gateway_tools:
        tools = list(gateway_tools)
    elif mcp_factory and gateway_url:
        try:
            mcp_client = mcp_factory.create_client(gateway_url, tenant_id)
            tools = list(mcp_client.tools or [])
        except Exception as e:
            logger.warning("MCP factory failed: %s", e)

    # Every Gateway tool call is bound to the verified tenant, whatever the model asks for.
    gateway_tool_names = {t.tool_name for t in tools if getattr(t, "tool_name", None)}
    tenant_scope = TenantScopeHook(tenant_id, gateway_tool_names)

    code_interp = _get_code_interpreter_tool()
    if code_interp:
        tools.append(code_interp)

    # Memory session manager for conversation persistence
    memory_sm = _get_memory_session_manager(tenant_id, session_id)

    model = BedrockModel(model_id=MODEL_ID)
    agent = Agent(
        model=model,
        tools=tools,
        system_prompt=_build_system_prompt(tenant_id),
        conversation_manager=SlidingWindowConversationManager(window_size=25),
        session_manager=memory_sm,
        hooks=[tenant_scope],
    )

    return agent, session


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _extract_response(result, agent_instance=None) -> dict:
    """Extract text and images from a Strands agent result.

    Images from Code Interpreter are in toolResult content blocks within
    agent.messages. We scan agent.messages backwards per the AWS docs pattern:
      for message in agent.messages:
          for item in message.get('content', []):
              if isinstance(item, dict) and 'toolResult' in item:
                  for c in item['toolResult']['content']:
                      if c.get('type') == 'image':
                          image_bytes = c['image']['source']['bytes']
    """
    text = str(result)
    images = []

    # Debug: log what we have
    logger.info("_extract_response: agent_instance=%s, result type=%s",
                 type(agent_instance).__name__ if agent_instance else "None",
                 type(result).__name__)

    def _to_b64(img_data) -> str | None:
        import base64 as _b64
        if isinstance(img_data, bytes):
            return _b64.b64encode(img_data).decode("utf-8")
        elif isinstance(img_data, str) and len(img_data) > 100:
            return img_data
        return None

    # 1. Scan agent.messages for toolResult images (AWS docs pattern)
    if agent_instance:
        try:
            messages = getattr(agent_instance, "messages", []) or []
            logger.info("Scanning %d agent.messages for images", len(messages))
            # Log content types found
            for i, msg in enumerate(messages):
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                    types = []
                    for item in content:
                        if isinstance(item, dict):
                            if "toolResult" in item:
                                tr_types = [c.get("type", "?") for c in item["toolResult"].get("content", []) if isinstance(c, dict)]
                                types.append(f"toolResult({','.join(tr_types)})")
                            elif "text" in item:
                                types.append("text")
                            elif "toolUse" in item:
                                types.append("toolUse")
                            elif "image" in item:
                                types.append("IMAGE")
                            else:
                                types.append(str(list(item.keys())[:2]))
                    if types:
                        logger.info("  msg[%d] role=%s types=%s", i, msg.get("role", "?"), types)
            for msg in reversed(messages):
                if images:
                    break
                if not isinstance(msg, dict):
                    continue
                for item in msg.get("content", []):
                    if images:
                        break
                    if not isinstance(item, dict):
                        continue
                    if "toolResult" in item:
                        for c in item["toolResult"].get("content", []):
                            if isinstance(c, dict):
                                # Check for image type
                                if c.get("type") == "image" or c.get("image"):
                                    img_obj = c.get("image", c)
                                    src = img_obj.get("source", img_obj)
                                    img_bytes = src.get("bytes") or src.get("data")
                                    if img_bytes:
                                        b64 = _to_b64(img_bytes)
                                        if b64:
                                            images.append(b64)
                                            logger.info("Image from toolResult image block (%d chars)", len(b64))
                                            break
                                # Check for IMAGE_BASE64 markers in text content
                                text_val = c.get("text", "")
                                if isinstance(text_val, str) and "IMAGE_BASE64_START" in text_val:
                                    import re as _re
                                    m = _re.search(r"IMAGE_BASE64_START([A-Za-z0-9+/=\s]+?)IMAGE_BASE64_END", text_val)
                                    if m:
                                        img_data = m.group(1).replace("\n", "").replace(" ", "")
                                        images.append(img_data)
                                        logger.info("Image from toolResult text markers (%d chars)", len(img_data))
                                        break
                    if "image" in item and not images:
                        src = item["image"].get("source", {})
                        img_bytes = src.get("bytes") or src.get("data")
                        if img_bytes:
                            b64 = _to_b64(img_bytes)
                            if b64:
                                images.append(b64)
                                logger.info("Image from agent.messages direct (%d chars)", len(b64))
                                break
        except Exception as e:
            logger.warning("agent.messages scan: %s", e)

    # 2. Fallback: scan result.message
    if not images:
        try:
            message = getattr(result, "message", None) or {}
            for block in (message.get("content", []) if isinstance(message, dict) else []):
                if isinstance(block, dict) and block.get("image"):
                    src = block["image"].get("source", {})
                    img_bytes = src.get("bytes") or src.get("data")
                    if img_bytes:
                        b64 = _to_b64(img_bytes)
                        if b64:
                            images.append(b64)
                            break
        except Exception as e:
            logger.debug("result.message scan: %s", e)

    # 3. Fallback: base64 markers in stdout text
    if not images:
        import re as _re
        match = _re.search(r"IMAGE_BASE64_START([A-Za-z0-9+/=\s]+?)IMAGE_BASE64_END", text)
        if match:
            img_data = match.group(1).replace("\n", "").replace(" ", "")
            images.append(img_data)
            text = text[:match.start()] + text[match.end():]
            logger.info("Image from START/END markers (%d chars)", len(img_data))

    logger.info("Total images extracted: %d", len(images))

    resp = {"response": text}
    if images:
        resp["image_base64"] = images[0]
    return resp


async def handle_request(payload, context: RequestContext):
    """AgentCore Runtime entrypoint.

    The SDK passes the RequestContext only when the second parameter is named
    ``context``. *payload* is the JSON request body and is fully
    client-controlled, so it is never a source of identity. The tenant comes
    from the caller's verified access token (the Authorization header in
    *context*). The request fails closed if the tenant can't be verified.
    """
    try:
        tenant_id = resolve_tenant_id(getattr(context, "request_headers", None))
    except TenantResolutionError as e:
        logger.warning("Rejected request: tenant identity could not be verified (%s)", e)
        return {"error": "unauthorized",
                "response": "We couldn't verify your tenant identity. Please sign in again."}

    if not isinstance(payload, dict):
        return {"error": "invalid_request", "response": "The request body must be a JSON object."}
    input_text = payload.get("input_text") or payload.get("prompt") or ""
    if not isinstance(input_text, str) or not input_text.strip():
        return {"response": "Please provide a prompt."}

    session_id = getattr(context, "session_id", None) or str(uuid.uuid4())

    session = get_or_create_session(tenant_id, session_id)
    logger.info("tenant=%s session=%s prompt=%s", tenant_id, session_id, input_text[:100])

    # Load Gateway tools via MCP client (connection must stay open during agent run).
    # Signing the tenant assertion is part of creating the client. If it fails, the
    # request is refused: running without Gateway tools would hide a broken setup.
    try:
        mcp_client = get_mcp_client(tenant_id)
    except TenantResolutionError as e:
        logger.error("Rejected request: could not sign tenant assertion (%s)", e)
        return {"error": "unavailable",
                "response": "Billing data is temporarily unavailable. Please try again later."}
    except Exception as e:
        logger.warning("Failed to create MCP client: %s", e)
        mcp_client = None

    if mcp_client:
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            logger.info("Loaded %d tools from Gateway", len(tools))

            agent, _session = create_billing_agent(
                tenant_id=tenant_id,
                session_id=session_id,
                gateway_url=GATEWAY_URL,
                gateway_tools=tools,
            )

            result = agent(input_text)
            return _extract_response(result, agent_instance=agent)

    # No gateway configured — agent runs with Code Interpreter / Memory only
    agent, _session = create_billing_agent(
        tenant_id=tenant_id,
        session_id=session_id,
        gateway_url=GATEWAY_URL,
    )

    result = agent(input_text)
    return _extract_response(result, agent_instance=agent)


# Register with AgentCore Runtime
app.entrypoint(handle_request)


if __name__ == "__main__":
    app.run()
