"""BillingMemoryHook — AgentCore Memory integration for the billing agent.

Implements the ``HookProvider`` interface to automatically persist and
retrieve conversation context on every agent turn.  All memory operations
are scoped by ``actor_id=tenant_id`` and ``session_id`` to enforce strict
tenant isolation.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# SDK imports with local stubs as fallback
# ---------------------------------------------------------------------------

try:
    from strands.hooks import HookProvider  # type: ignore[import-untyped]
except (ImportError, ModuleNotFoundError):

    class HookProvider:  # type: ignore[no-redef]
        """Minimal stub for ``strands.hooks.HookProvider``.

        The real Strands SDK provides this base class for agent lifecycle
        hooks.  When the SDK is available this stub is unused.
        """

        def on_agent_start(self, agent_context: Any) -> None:  # pragma: no cover
            ...

        def on_agent_end(self, agent_context: Any, response: Any) -> None:  # pragma: no cover
            ...


try:
    from bedrock_agentcore.memory.client import MemoryClient  # type: ignore[import-untyped]
except (ImportError, ModuleNotFoundError):
    try:
        from bedrock_agentcore.services.memory import MemoryClient  # type: ignore[import-untyped]
    except (ImportError, ModuleNotFoundError):

        class MemoryClient:  # type: ignore[no-redef]
            """Minimal stub for ``bedrock_agentcore.services.memory.MemoryClient``.

            Provides the ``create_event`` and ``get_last_k_turns`` interface
            expected by the billing agent.  When the real AgentCore Memory SDK
            module is available this stub is unused.
            """

            def create_event(
                self,
                *,
                actor_id: str,
                session_id: str,
                event_data: dict[str, Any],
            ) -> dict[str, Any]:  # pragma: no cover
                return {}

            def get_last_k_turns(
                self,
                *,
                actor_id: str,
                session_id: str,
                k: int = 10,
            ) -> list[dict[str, Any]]:  # pragma: no cover
                return []


# Default number of recent turns to load from short-term memory
DEFAULT_K_TURNS = 10


class BillingMemoryHook(HookProvider):
    """Integrates AgentCore Memory into the billing agent conversation loop.

    On every agent turn:
    * ``on_agent_start`` — loads the last *k* conversation turns from
      short-term memory (STM) and prepends them to the agent context.
    * ``on_agent_end`` — persists the current conversation turn (input +
      output) to STM via ``create_event``.

    All operations are scoped by ``actor_id=tenant_id`` and ``session_id``
    to guarantee tenant isolation (Requirement 4.6).
    """

    def __init__(
        self,
        tenant_id: str,
        session_id: str,
        memory_client: MemoryClient | None = None,
        k: int = DEFAULT_K_TURNS,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        if not session_id:
            raise ValueError("session_id must be a non-empty string")

        self.tenant_id = tenant_id
        self.session_id = session_id
        self.memory_client = memory_client or MemoryClient()
        self.k = k

    # -- HookProvider lifecycle callbacks -----------------------------------

    def on_agent_start(self, agent_context: Any) -> None:
        """Load recent conversation turns from STM before the agent processes input.

        Calls ``MemoryClient.get_last_k_turns`` scoped to the current
        tenant and session, then prepends the returned messages to the
        agent context so the model has conversational history.

        Requirements: 4.5, 4.6
        """
        turns = self.memory_client.get_last_k_turns(
            actor_id=self.tenant_id,
            session_id=self.session_id,
            k=self.k,
        )
        if hasattr(agent_context, "prepend_messages"):
            agent_context.prepend_messages(turns)

    def on_agent_end(self, agent_context: Any, response: Any) -> None:
        """Persist the current conversation turn to STM after the agent responds.

        Calls ``MemoryClient.create_event`` scoped to the current tenant
        and session with the input text and agent output.

        Requirements: 4.4, 4.6
        """
        input_text = ""
        if hasattr(agent_context, "input_text"):
            input_text = agent_context.input_text

        self.memory_client.create_event(
            actor_id=self.tenant_id,
            session_id=self.session_id,
            event_data={
                "input": input_text,
                "output": str(response),
            },
        )
