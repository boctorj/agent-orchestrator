"""Tests for orchestrator/mcp_launcher.py — minimum-env launcher.

The launcher exec's into the actual MCP server, so we don't call main()
in tests (it would replace pytest's process). We test the env-building
helper instead.
"""

from __future__ import annotations

import os

from orchestrator import mcp_launcher


def test_allowlist_includes_essentials():
    """If anyone removes HOME or PATH, things break in real use."""
    must_have = {"HOME", "PATH", "USERPROFILE"}
    assert must_have <= set(mcp_launcher.ALLOWLIST)


def test_build_min_env_drops_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-pass")
    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    new_env = mcp_launcher.build_min_env()

    # Secrets must NOT pass through
    assert "ANTHROPIC_API_KEY" not in new_env
    assert "GITHUB_TOKEN" not in new_env
    assert "AWS_SECRET_ACCESS_KEY" not in new_env

    # Allowlisted vars do pass through
    assert new_env["HOME"] == "/home/test"
    assert new_env["PATH"] == "/usr/bin:/bin"


def test_build_min_env_forces_utf8(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    new_env = mcp_launcher.build_min_env()
    assert new_env["PYTHONIOENCODING"] == "utf-8"


def test_build_min_env_overrides_existing_pythonioencoding(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    new_env = mcp_launcher.build_min_env()
    assert new_env["PYTHONIOENCODING"] == "utf-8"


def test_build_min_env_prepends_cwd_to_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    new_env = mcp_launcher.build_min_env()
    assert os.getcwd() in new_env["PYTHONPATH"]


def test_build_min_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/some/lib")
    new_env = mcp_launcher.build_min_env()
    # cwd should be prepended, original kept after
    parts = new_env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == os.getcwd()
    assert "/some/lib" in parts


def test_build_min_env_handles_missing_optional_vars(monkeypatch):
    """Launcher shouldn't crash if HOME / TMPDIR / etc. are unset."""
    for var in mcp_launcher.ALLOWLIST:
        monkeypatch.delenv(var, raising=False)
    new_env = mcp_launcher.build_min_env()
    # Only the forced ones should be present
    assert new_env["PYTHONIOENCODING"] == "utf-8"
    assert "PYTHONPATH" in new_env  # always set to at least cwd
