"""Anthropic Managed Agents implementation of the `Worker` protocol.

This is the v1 backend — drives an Anthropic Managed Agent session for one
role (coder/tester/reviewer). Caches the agent + environment IDs across
spawns within one process and across processes via `state.cached_resources`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import anthropic
from anthropic import Anthropic

from orchestrator.workers.base import TailMessage, TailResult, TailStatus, _validate_limit

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
DEFAULT_MODEL = "claude-opus-4-7"

# Hosts the agent container is allowed to reach. Extend per-org if you add
# private package registries, internal artifact stores, etc.
ALLOWED_NETWORK_HOSTS = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "api.anthropic.com",
]

# Default Environment config used for every role's container.
# `limited` networking + `allow_package_managers: True` is the right balance:
# agents can still clone, push, install deps; can NOT exfiltrate to random URLs.
DEFAULT_ENV_CONFIG: dict = {
    "type": "cloud",
    "networking": {
        "type": "limited",
        "allowed_hosts": ALLOWED_NETWORK_HOSTS,
        "allow_package_managers": True,  # PyPI, npm, RubyGems, cargo, Go modules
        "allow_mcp_servers": False,  # agents don't have MCP servers; bypass not needed
    },
}

DEFAULT_TOOLS: list[dict] = [{"type": "agent_toolset_20260401"}]


def load_role_prompt(role: str) -> str:
    return (PROMPTS_DIR / f"{role}.md").read_text()


# Map Anthropic's session.status enum onto the four-value taxonomy the
# ``Worker.tail_messages`` protocol exposes. The provider has four states
# today (``rescheduling | running | idle | terminated``); ``rescheduling``
# is still in-flight from the caller's perspective so we collapse it onto
# ``running`` rather than introducing a fifth status callers must handle.
_MANAGED_STATUS_MAP: dict[str, TailStatus] = {
    "idle": "idle",
    "running": "running",
    "rescheduling": "running",
    "terminated": "terminated",
}


def _map_managed_status(raw: str) -> TailStatus:
    """Translate a raw Anthropic session.status into a ``TailStatus``.

    Unknown statuses fall back to ``running`` — NOT ``idle``. The
    ``tail_worker`` MCP tool's whole point is to stop the user waiting
    on a hung worker; misreporting an unknown future state (e.g. a new
    ``requires_action`` Anthropic ships later) as ``idle`` would tell
    callers "work finished, safe to archive" when it actually hasn't.
    ``running`` keeps callers in the wait/poll loop, which is safe.

    The caller is expected to also populate ``TailResult.reason`` with
    the raw status string whenever this falls back, so observability
    isn't lost. See ``tail_messages``.
    """
    return _MANAGED_STATUS_MAP.get(raw, "running")


def _format_ts(event: object) -> str:
    """Render an event's ``processed_at`` timestamp as a string.

    Anthropic SDK events expose ``processed_at`` as a ``datetime``; some
    fakes pass a plain string. Handle both without raising.
    """
    raw = getattr(event, "processed_at", None)
    if raw is None:
        return ""
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return str(raw)


def _resource_signature(role: str, prompt: str, model: str) -> str:
    """16-char sha256 over the things that actually change in practice.

    Cache key dimensions:
      - role:   different roles get different agents
      - prompt: edits to coder.md / tester.md / reviewer.md must take effect
      - model:  bumping DEFAULT_MODEL must take effect

    NOT in the signature (intentionally):
      - env_config (networking): static; doesn't change between runs
      - tools list: static; if you ever change it, run reset_cached_resources

    Staleness from external causes (Anthropic improvements over time, etc.)
    is handled by TTL — see state.get_cached_resource(max_age_days=...).
    """
    blob = json.dumps(
        {"role": role, "prompt": prompt, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class ManagedAgentWorker:
    """Drives an Anthropic Managed Agent session for one role (coder/tester/reviewer).

    Caches the agent + environment IDs across spawns within one process.
    Cross-process persistence comes in Stage 2 via the state DB.
    """

    # Bound the poll loop in `_wait_for_session_active`. Class attributes so
    # tests can monkeypatch them down to near-zero without changing call sites.
    # 60s: sessions idle for hours need more server-side state-update time
    # before the SSE stream will see activity (10s was too short in practice).
    SESSION_ACTIVE_POLL_TIMEOUT = 60.0
    SESSION_ACTIVE_POLL_INTERVAL = 0.3

    def __init__(self, role: str, *, model: str = DEFAULT_MODEL):
        self.role = role
        self.model = model
        self.client = Anthropic()
        self._agent_id: str | None = None
        self._env_id: str | None = None

    def _ensure_resources(self) -> None:
        # Import here to avoid circular import at module load
        from orchestrator import state

        if self._agent_id is not None and self._env_id is not None:
            return

        prompt = load_role_prompt(self.role)
        sig = _resource_signature(self.role, prompt, self.model)

        cached = state.get_cached_resource(self.role, sig)
        if cached:
            self._agent_id, self._env_id = cached
            return

        # Create fresh agent + environment, persist for next time.
        # The signature (sig) goes in resource names so you can see in the
        # Anthropic console which prompt/config produced which agent.
        # The Anthropic SDK uses strict TypedDicts for tools/config that
        # don't quite match our dict literals; the per-line type: ignore
        # comments below suppress those (behavior is correct).
        agent = self.client.beta.agents.create(
            name=f"orchestrator-{self.role}-{sig}",
            model=self.model,
            system=prompt,
            tools=DEFAULT_TOOLS,  # type: ignore[arg-type]
        )
        env = self.client.beta.environments.create(
            name=f"orchestrator-env-{self.role}-{sig}",
            config=DEFAULT_ENV_CONFIG,  # type: ignore[arg-type]
        )
        self._agent_id = agent.id
        self._env_id = env.id
        state.save_cached_resource(self.role, sig, agent.id, env.id)

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        self._ensure_resources()
        assert self._agent_id is not None and self._env_id is not None
        session = self.client.beta.sessions.create(
            agent=self._agent_id,
            environment_id=self._env_id,
            title=title or f"{self.role}: {task[:60]}",
        )
        return session.id, self._send_and_collect(session.id, task)

    def spawn_async(self, task: str, *, title: str | None = None) -> str:
        """Create a session, send the initial user message, return session_id.

        Does NOT wait for the agent to finish. Use wait_idle(session_id) later
        to collect the response. Enables parallel unit execution.
        """
        self._ensure_resources()
        assert self._agent_id is not None and self._env_id is not None
        session = self.client.beta.sessions.create(
            agent=self._agent_id,
            environment_id=self._env_id,
            title=title or f"{self.role}: {task[:60]}",
        )
        self.client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": task}]}],
        )
        return session.id

    def wait_idle(self, session_id: str, *, timeout_seconds: int = 1800) -> str:
        """Stream events from a running session until session.status_idle.

        Returns the concatenated agent.message text. Raises TimeoutError
        after timeout_seconds. Default 30 minutes per session.
        """
        import time

        text_parts: list[str] = []
        saw_activity = False
        deadline = time.time() + timeout_seconds
        with self.client.beta.sessions.events.stream(session_id) as stream:
            for event in stream:
                if time.time() > deadline:
                    raise TimeoutError(
                        f"Session {session_id} did not idle within {timeout_seconds}s"
                    )
                etype = getattr(event, "type", None)
                # A freshly-created session is `idle` until our user.message
                # transitions it to running. The stream may emit that initial
                # status_idle before the message lands; treat status_idle as
                # completion only after observing any other event.
                if etype == "session.status_idle":
                    if saw_activity:
                        break
                    continue
                saw_activity = True
                if etype == "agent.message":
                    for block in getattr(event, "content", []):
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
        return "".join(text_parts)

    def resume(self, session_id: str, msg: str) -> str:
        return self._send_and_collect(session_id, msg)

    def archive(self, session_id: str) -> None:
        self.client.beta.sessions.archive(session_id)

    def _wait_for_session_active(self, session_id: str) -> None:
        """Poll until the session's status transitions away from ``idle``.

        PR #23 sent the ``user.message`` before opening the SSE stream so
        fresh sessions had time to flip ``idle`` → ``running`` before the
        subscribe. That works for brand-new sessions, but on long-idle
        resumes (session that's been idle for hours) the server-side
        send → state-update window is longer; the stream can still open
        against an ``idle`` session, get a zero-event close, and return
        empty. Symptom: F-006-U-2 went to escalated with
        ``fix_no_marker`` 4s after ``address_review`` on a session idle
        ~15h.

        Best-effort: retrieve errors and timeout both fall through to the
        existing stream-open path so a genuinely stuck session still
        surfaces as ``coder_no_marker`` / ``fix_no_marker`` rather than
        hanging this method.
        """
        import time

        deadline = time.time() + self.SESSION_ACTIVE_POLL_TIMEOUT
        polls = 0
        while time.time() < deadline:
            try:
                session = self.client.beta.sessions.retrieve(session_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "session_active_poll_error session=%s poll=%d error=%r — proceeding to stream",
                    session_id,
                    polls,
                    e,
                )
                return
            status = getattr(session, "status", None)
            if status != "idle":
                logger.debug(
                    "session_active session=%s status=%s polls=%d",
                    session_id,
                    status,
                    polls,
                )
                return
            polls += 1
            time.sleep(self.SESSION_ACTIVE_POLL_INTERVAL)
        logger.warning(
            "session_active_timeout session=%s still_idle after %.1fs (%d polls) — proceeding to stream",
            session_id,
            self.SESSION_ACTIVE_POLL_TIMEOUT,
            polls,
        )

    def tail_messages(self, session_id: str, *, limit: int = 50) -> TailResult:
        """Return the session's status + its most recent agent messages.

        Two SDK calls in sequence:
          1. ``sessions.retrieve`` for the status (same field
             ``resume_unit`` surfaces today).
          2. ``sessions.events.list`` for the recent ``agent.message``
             events. Asked for ``order='desc'`` so the cursor yields the
             newest events first; we collect up to ``limit`` and reverse
             so the caller sees them chronologically.

        ``NotFoundError`` from ``retrieve`` short-circuits with
        ``status='not_found'`` and an empty messages list — ``events.list``
        is NOT called in that branch (no point asking for events on a
        session the provider doesn't know about). Other API failures
        bubble up unchanged so the caller sees real outages rather than
        a silent empty tail.

        ``reason`` is populated whenever the raw provider status is
        outside the documented enum (today: ``rescheduling | running |
        idle | terminated``) so observability survives SDK drift, and
        whenever the mapped status is ``terminated``.
        """
        _validate_limit(limit)
        try:
            session = self.client.beta.sessions.retrieve(session_id)
        except anthropic.NotFoundError as exc:
            return {"status": "not_found", "messages": [], "reason": str(exc)}

        raw_status = getattr(session, "status", "unknown")
        status = _map_managed_status(raw_status)
        # Surface the raw status whenever it's worth carrying: terminated
        # (caller wants the cause) or an unknown SDK value (so observability
        # of provider-side drift isn't lost). Known mappings (idle/running/
        # rescheduling) get reason=None on the happy path.
        reason: str | None = None
        if status == "terminated" or raw_status not in _MANAGED_STATUS_MAP:
            reason = f"session.status={raw_status}"

        events = self.client.beta.sessions.events.list(
            session_id,
            types=["agent.message"],
            limit=limit,
            order="desc",
        )
        collected: list[TailMessage] = []
        for ev in events:
            if getattr(ev, "type", None) != "agent.message":
                continue
            text = "".join(
                getattr(block, "text", "") or "" for block in (getattr(ev, "content", None) or [])
            )
            if not text:
                continue
            collected.append({"ts": _format_ts(ev), "role": "agent", "text": text})
            if len(collected) >= limit:
                break
        collected.reverse()  # chronological (events.list returns newest-first)
        return {"status": status, "messages": collected, "reason": reason}

    def _send_and_collect(self, session_id: str, msg: str) -> str:
        # Send the user.message BEFORE subscribing to the SSE event stream.
        # PR #21 added a `saw_activity` gate to ignore an initial spurious
        # `status_idle`. PR #23 then sent the message before opening the
        # stream so fresh sessions had time to flip to `running`. Both
        # are necessary but not sufficient on long-idle resumes: the
        # server-side send → state-update window is wider on a session
        # that's been idle for hours, the SSE endpoint can still see an
        # `idle` session at subscribe-time and close the connection with
        # zero events. After send, poll `sessions.retrieve` until the
        # status flips away from `idle` before opening the stream.
        # The saw_activity gate stays as defense-in-depth in case the
        # agent finishes between send and stream-open on a very fast task.
        logger.info("send_and_collect_start session=%s msg_len=%d", session_id, len(msg))
        self.client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": msg}],
                }
            ],
        )
        logger.debug("send_and_collect_sent session=%s", session_id)
        self._wait_for_session_active(session_id)
        text_parts: list[str] = []
        saw_activity = False
        event_count = 0
        with self.client.beta.sessions.events.stream(session_id) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                event_count += 1
                logger.debug("send_and_collect_event session=%s type=%s", session_id, etype)
                if etype == "session.status_idle":
                    if saw_activity:
                        logger.debug(
                            "send_and_collect_done session=%s events=%d response_len=%d",
                            session_id,
                            event_count,
                            sum(len(p) for p in text_parts),
                        )
                        break
                    continue
                saw_activity = True
                if etype == "agent.message":
                    for block in getattr(event, "content", []):
                        text = getattr(block, "text", None)
                        if text:
                            text_parts.append(text)
        response = "".join(text_parts)
        if not response:
            logger.warning(
                "send_and_collect_empty session=%s events=%d saw_activity=%s — "
                "stream closed with no agent output; likely session_active_timeout fired too early",
                session_id,
                event_count,
                saw_activity,
            )
        else:
            logger.info(
                "send_and_collect_complete session=%s events=%d response_len=%d",
                session_id,
                event_count,
                len(response),
            )
        return response
