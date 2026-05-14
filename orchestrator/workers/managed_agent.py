"""Anthropic Managed Agents implementation of the `Worker` protocol.

This is the v1 backend — drives an Anthropic Managed Agent session for one
role (coder/tester/reviewer). Caches the agent + environment IDs across
spawns within one process and across processes via `state.cached_resources`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from anthropic import Anthropic

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

    def _send_and_collect(self, session_id: str, msg: str) -> str:
        with self.client.beta.sessions.events.stream(session_id) as stream:
            self.client.beta.sessions.events.send(
                session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": msg}],
                    }
                ],
            )
            text_parts: list[str] = []
            saw_activity = False
            for event in stream:
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
