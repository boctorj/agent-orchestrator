"""Tests for orchestrator/agents.py — pure functions (no Anthropic API calls)."""

from __future__ import annotations

from orchestrator import agents


class TestResourceSignature:
    def test_stable_for_same_inputs(self):
        a = agents._resource_signature("coder", "prompt", "model")
        b = agents._resource_signature("coder", "prompt", "model")
        assert a == b

    def test_changes_when_role_changes(self):
        a = agents._resource_signature("coder", "p", "m")
        b = agents._resource_signature("tester", "p", "m")
        assert a != b

    def test_changes_when_prompt_changes(self):
        a = agents._resource_signature("coder", "p1", "m")
        b = agents._resource_signature("coder", "p2", "m")
        assert a != b

    def test_changes_when_model_changes(self):
        a = agents._resource_signature("coder", "p", "m1")
        b = agents._resource_signature("coder", "p", "m2")
        assert a != b

    def test_signature_is_16_chars(self):
        sig = agents._resource_signature("r", "p", "m")
        assert len(sig) == 16
        # All hex chars
        assert all(c in "0123456789abcdef" for c in sig)


def test_default_model_is_set():
    assert agents.DEFAULT_MODEL  # non-empty
    assert "claude" in agents.DEFAULT_MODEL


def test_allowed_network_hosts_includes_essentials():
    """If anyone removes github.com from the allowlist, agents stop working."""
    must_have = {"github.com", "api.github.com", "api.anthropic.com"}
    assert must_have <= set(agents.ALLOWED_NETWORK_HOSTS)


def test_default_env_config_uses_limited_networking():
    """Defense-in-depth: never default to unrestricted."""
    assert agents.DEFAULT_ENV_CONFIG["networking"]["type"] == "limited"
    assert agents.DEFAULT_ENV_CONFIG["networking"]["allow_mcp_servers"] is False
