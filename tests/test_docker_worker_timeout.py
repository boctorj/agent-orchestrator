"""Subprocess-timeout tests for the Docker worker (F-001-U-6).

PR #11 reviewer SUGGESTION 1 noted that `DockerClaudeCodeWorker.spawn`
and `.resume` called `subprocess.run` without a `timeout=`, so a hung
`docker run` would block the orchestrator thread forever. The doctor
probes already use 10s/30s timeouts; this file pins the parallel
behaviour on the real spawn/resume path:

  * Default timeout is `DEFAULT_SPAWN_TIMEOUT_SECONDS` (30 min).
  * `ORCH_WORKER_TIMEOUT_SECONDS` env knob overrides the default.
  * Constructor field `timeout_seconds=` overrides the env knob.
  * Garbage env values (negative, non-numeric) fall back to the default
    rather than crashing the spawn.
  * `subprocess.TimeoutExpired` is translated into a `RuntimeError`
    that names the timeout budget, so the orchestrator's escalation
    surface sees a clean failure instead of a raw subprocess exception.

The E2E counterpart in `tests/e2e/test_docker_worker_smoke.py` drives
a real `docker run` with a hanging in-container command and asserts the
timeout fires within budget; here we mock the subprocess seam.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from orchestrator.workers.docker_claude_code import (
    DEFAULT_SPAWN_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
    DockerClaudeCodeWorker,
    _resolve_timeout_seconds,
)
from tests.conftest import _FakeProc, _make_worker

# ---------------------------------------------------------------------------
# _resolve_timeout_seconds: pure resolution-order tests.
# ---------------------------------------------------------------------------


class TestResolveTimeoutSeconds:
    def test_override_wins_over_env_and_default(self):
        assert _resolve_timeout_seconds(10, {TIMEOUT_ENV: "60"}) == 10

    def test_env_used_when_no_override(self):
        assert _resolve_timeout_seconds(None, {TIMEOUT_ENV: "120"}) == 120

    def test_default_used_when_neither_set(self):
        assert _resolve_timeout_seconds(None, {}) == DEFAULT_SPAWN_TIMEOUT_SECONDS

    def test_invalid_env_value_falls_back_to_default(self):
        """A typo in `.env` must not crash a real spawn."""
        assert _resolve_timeout_seconds(None, {TIMEOUT_ENV: "not-a-number"}) == (
            DEFAULT_SPAWN_TIMEOUT_SECONDS
        )

    def test_non_positive_env_value_falls_back_to_default(self):
        assert _resolve_timeout_seconds(None, {TIMEOUT_ENV: "0"}) == DEFAULT_SPAWN_TIMEOUT_SECONDS
        assert _resolve_timeout_seconds(None, {TIMEOUT_ENV: "-30"}) == DEFAULT_SPAWN_TIMEOUT_SECONDS

    def test_non_positive_override_falls_back_to_env_then_default(self):
        """`timeout_seconds=0` should be ignored rather than disabling the timeout."""
        assert _resolve_timeout_seconds(0, {TIMEOUT_ENV: "45"}) == 45
        assert _resolve_timeout_seconds(-1, {}) == DEFAULT_SPAWN_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# spawn() / resume() pass timeout= through to the subprocess runner.
# ---------------------------------------------------------------------------


class TestSpawnResumePassTimeout:
    def test_spawn_default_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(TIMEOUT_ENV, raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.spawn("task")
        assert captured["timeout"] == DEFAULT_SPAWN_TIMEOUT_SECONDS

    def test_spawn_env_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(TIMEOUT_ENV, "90")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.spawn("task")
        assert captured["timeout"] == 90

    def test_spawn_constructor_timeout(self, tmp_path, monkeypatch):
        """`timeout_seconds=` field beats the env knob — tests use this
        to drive a 10s timeout against a hanging in-container command."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(TIMEOUT_ENV, "9999")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 10
        worker.run = fake_run
        worker.spawn("task")
        assert captured["timeout"] == 10

    def test_resume_passes_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(TIMEOUT_ENV, raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 15
        worker.run = fake_run
        worker.resume("sess-1", "next")
        assert captured["timeout"] == 15


# ---------------------------------------------------------------------------
# TimeoutExpired -> RuntimeError translation. Orchestrator escalation
# surfaces a clean error message naming the budget, not a raw subprocess
# exception. The test asserts the error fires within wall-clock budget
# so the timeout actually short-circuits the call.
# ---------------------------------------------------------------------------


class TestTimeoutTranslation:
    def test_spawn_timeout_raises_runtime_error_with_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging_run(argv, **kwargs):
            # Simulate the real subprocess.run timeout path: raise
            # subprocess.TimeoutExpired after the configured budget.
            timeout = kwargs.get("timeout") or 0
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 7
        worker.run = hanging_run

        start = time.monotonic()
        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("hi")
        elapsed = time.monotonic() - start

        msg = str(excinfo.value)
        assert "timed out" in msg
        assert "7s" in msg, f"error must surface the budget; got: {msg!r}"
        assert worker.role in msg
        # The translation runs the moment the fake raises; the assert is
        # there to make sure we didn't silently swallow the timeout.
        assert elapsed < 2.0

    def test_resume_timeout_raises_runtime_error_with_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 5
        worker.run = hanging_run

        with pytest.raises(RuntimeError) as excinfo:
            worker.resume("sess-1", "msg")
        assert "timed out" in str(excinfo.value)
        assert "5s" in str(excinfo.value)


# ---------------------------------------------------------------------------
# `timeout_seconds` defaults to None on construction (back-compat).
# ---------------------------------------------------------------------------


def test_timeout_seconds_default_is_none(tmp_path):
    """The new field defaults to None so existing call-sites don't need
    to pass it. The resolution helper backfills the default value."""
    worker = _make_worker(tmp_path)
    assert worker.timeout_seconds is None


def test_worker_dataclass_accepts_timeout_seconds_kwarg(tmp_path):
    """Constructor accepts the new field by keyword."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    w = DockerClaudeCodeWorker(
        role="coder", workdir=workdir, home_dir=fake_home, timeout_seconds=42
    )
    assert w.timeout_seconds == 42
