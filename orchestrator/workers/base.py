"""Worker protocol — the seam for swapping execution backends.

The `Worker` protocol is what every backend (Anthropic Managed Agents,
Docker + Claude Code, future LLMs) must satisfy. Everything downstream
of the factory (`cycle_review`, gate logic, prompts, dashboard) sees an
opaque `Worker` and doesn't care which implementation answered.

See `docs/PROPOSAL-docker-workers.md` for the broader design.
"""

from __future__ import annotations

from typing import Protocol


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
