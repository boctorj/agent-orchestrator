"""Worker protocol — the seam for swapping execution backends.

The `Worker` protocol is what every backend (Anthropic Managed Agents,
Docker + Claude Code, future LLMs) must satisfy. Everything downstream
of the factory (`cycle_review`, gate logic, prompts, dashboard) sees an
opaque `Worker` and doesn't care which implementation answered.

See `docs/PROPOSAL-docker-workers.md` for the broader design.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict

# Status taxonomy shared by every backend's ``tail_messages`` implementation.
# Kept narrow on purpose — callers (the future ``tail_worker`` MCP tool in
# F-008) branch on exactly these four values, and richer detail goes in the
# ``reason`` field rather than expanding this enum.
TailStatus = Literal["running", "idle", "terminated", "not_found"]


class TailMessage(TypedDict):
    """One recent ``agent.message`` event, normalized across backends.

    Both backends filter to assistant output before emitting — the
    managed-agents backend via ``types=['agent.message']`` (hardcodes
    ``role='agent'``); the docker backend by restricting the JSONL
    parser to records whose outer ``type`` / inner ``role`` is
    ``'assistant'`` / ``'agent'``. User prompts and tool turns are
    NEVER returned. Callers can therefore assume ``role`` is one of
    ``{'assistant', 'agent'}`` and treat the two as synonymous.
    """

    ts: str
    role: str
    text: str


class TailResult(TypedDict):
    """Return type of ``Worker.tail_messages``.

    Fields:
      * ``status``   — coarse session/container state (see ``TailStatus``).
      * ``messages`` — most-recent ``agent.message`` events in chronological
        order. Capped at the caller's ``limit``. Empty when the backend
        can't see any (e.g. ``not_found``, or a terminated session that
        crashed before emitting output).
      * ``reason``   — free-form context, populated whenever the status
        choice was driven by something the caller may want to surface
        (raw provider status that didn't map cleanly, container exit
        code, inspect-failure stderr, NotFound error text). ``None`` on
        the canonical happy paths (``running``, ``idle``, plain
        ``not_found``).
    """

    status: TailStatus
    messages: list[TailMessage]
    reason: str | None


class Worker(Protocol):
    role: str

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        """Create a session for this role and send the initial task.

        Returns (session_id, final_assistant_text).
        """
        ...

    def resume(self, session_id: str, msg: str) -> str:
        """Send a follow-up message to an existing session. Returns final text."""
        ...

    def archive(self, session_id: str) -> None:
        """Mark a session as done; preserves history but blocks new events."""
        ...

    def tail_messages(self, session_id: str, *, limit: int = 50) -> TailResult:
        """Return the session's current status + its most recent agent messages.

        Used by the F-008 ``tail_worker`` MCP tool to stream a worker's
        recent output mid-cycle without waiting for it to idle. Every
        backend exposes the same shape (``TailResult``) so callers can
        branch on ``status`` without knowing which backend answered.

        Args:
            session_id: The backend-specific session identifier returned
                by ``spawn``.
            limit: Maximum number of ``agent.message`` events to return.
                Must be ``>= 1`` — implementations raise ``ValueError``
                otherwise. Messages are returned in chronological order
                (oldest first); if more than ``limit`` exist, the most-
                recent ``limit`` are kept.

        Returns:
            ``TailResult``. ``status='not_found'`` when the backend has
            no record of ``session_id``; ``messages`` is empty in that
            case.

        Raises:
            ValueError: if ``limit < 1``.
        """
        ...


def _validate_limit(limit: int) -> None:
    """Common pre-condition for ``tail_messages`` implementations.

    Lives at the protocol layer so the two backends can't drift on edge
    cases (``limit=0`` returned everything from one and nothing from the
    other before this gate). Implementations call this as the first
    statement of ``tail_messages``.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"tail_messages limit must be an int >= 1, got {limit!r}")
