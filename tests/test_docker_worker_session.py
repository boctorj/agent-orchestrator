"""Session-continuity tests for the Docker worker.

Two behaviors under test:

  1. `extract_session_id` reliably pulls the session id from
     `claude -p`'s stdout across the formats Claude Code's CLI emits.
  2. `DockerClaudeCodeWorker.resume(session_id, msg)` renders a
     `claude --resume <session_id> -p <msg>` command inside the
     container — the contract that lets the orchestrator pick up where
     a worker left off after a `docker run --rm` exits.

Subprocess is mocked end-to-end so no real Docker daemon is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    DockerClaudeCodeWorker,
    extract_session_id,
)
from tests.conftest import _FakeProc, _make_worker

# ---------------------------------------------------------------------------
# extract_session_id — formats it must handle.
# ---------------------------------------------------------------------------


class TestExtractSessionId:
    def test_single_document_json(self):
        out = json.dumps({"session_id": "sess-abc123", "result": "hello"})
        assert extract_session_id(out) == "sess-abc123"

    def test_session_id_camelcase_key(self):
        """Some CLI versions emit `sessionId` instead of `session_id`."""
        out = json.dumps({"sessionId": "sess-deadbeef", "result": "x"})
        assert extract_session_id(out) == "sess-deadbeef"

    def test_jsonl_stream_picks_up_session_id(self):
        """When claude streams JSON-Lines, the session id may sit on any
        line — usually the first 'init' message. Verify it's picked up
        even when surrounded by other JSON objects."""
        lines = [
            json.dumps({"type": "system", "session_id": "sess-xyz", "model": "claude"}),
            json.dumps({"type": "assistant", "text": "partial"}),
            json.dumps({"type": "result", "result": "done"}),
        ]
        out = "\n".join(lines)
        assert extract_session_id(out) == "sess-xyz"

    def test_plaintext_fallback(self):
        """Some older or non-JSON modes emit a human-readable session
        marker; verify the regex fallback still finds it."""
        out = "Created session_id: 99a8b7c6-test\nrunning..."
        assert extract_session_id(out) == "99a8b7c6-test"

    def test_returns_none_when_no_session_id_present(self):
        out = "Some unrelated output without any session marker.\n"
        assert extract_session_id(out) is None

    def test_returns_none_on_empty_stdout(self):
        assert extract_session_id("") is None
        assert extract_session_id("   \n  \n") is None

    def test_ignores_session_id_with_empty_value(self):
        """An empty `session_id` should not be treated as a valid id —
        callers depend on the return value being usable for --resume."""
        out = json.dumps({"session_id": "", "result": "x"})
        # Should fall through to JSONL / regex paths, find nothing, return None
        assert extract_session_id(out) is None


# ---------------------------------------------------------------------------
# resume() rendering — the docker argv must drive `claude --resume`.
# ---------------------------------------------------------------------------


@pytest.fixture
def worker(tmp_path: Path) -> DockerClaudeCodeWorker:
    # Thin wrapper around the shared `_make_worker` helper so the existing
    # fixture name (`worker`) keeps working in the parameterized tests below.
    return _make_worker(tmp_path)


class TestResumeRenders:
    def test_resume_argv_includes_resume_and_session_id(self, worker, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return _FakeProc(
                stdout=json.dumps({"session_id": "sess-1", "result": "ok"}),
                returncode=0,
            )

        worker.run = fake_run
        worker.resume("sess-1", "next message")

        argv = captured["argv"]
        # `--resume <id>` must appear adjacent in the argv (claude flag form).
        assert "--resume" in argv, f"resume flag missing: {argv!r}"
        idx = argv.index("--resume")
        assert argv[idx + 1] == "sess-1", (
            f"--resume value should be the session id; got argv[{idx + 1}]={argv[idx + 1]!r}"
        )
        # The follow-up message MUST be passed (claude needs `-p <msg>`).
        assert "-p" in argv, f"-p flag missing on resume: {argv!r}"
        p_idx = argv.index("-p")
        assert argv[p_idx + 1] == "next message"

    def test_spawn_then_resume_threads_session_id(self, worker, monkeypatch):
        """End-to-end: spawn extracts a session id; the subsequent
        resume call passes that exact id back into the container."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _FakeProc(
                stdout=json.dumps({"session_id": "sess-threaded", "result": "spawn-or-resume"}),
                returncode=0,
            )

        worker.run = fake_run
        session_id, _resp = worker.spawn("kick off work")
        worker.resume(session_id, "do another step")

        # Two docker runs: spawn (no --resume), resume (with --resume sess-threaded)
        assert len(calls) == 2
        spawn_argv, resume_argv = calls
        assert "--resume" not in spawn_argv
        assert "--resume" in resume_argv
        assert resume_argv[resume_argv.index("--resume") + 1] == "sess-threaded"


# ---------------------------------------------------------------------------
# spawn() error surface — caller should see useful failures.
# ---------------------------------------------------------------------------


class TestSpawnErrors:
    def test_spawn_raises_when_docker_nonzero(self, worker, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        worker.run = lambda *a, **kw: _FakeProc(
            stdout="", stderr="docker: image not found", returncode=125
        )
        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("hi")
        assert "docker run failed" in str(excinfo.value)

    def test_spawn_raises_when_session_id_missing(self, worker, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        worker.run = lambda *a, **kw: _FakeProc(
            stdout="random output with no session marker", returncode=0
        )
        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("hi")
        assert "session_id" in str(excinfo.value)
