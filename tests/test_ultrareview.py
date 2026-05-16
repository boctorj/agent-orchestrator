"""Tests for orchestrator/ultrareview.py — the `/ultrareview` invocation primitive.

We inject the subprocess `spawn` callable plus `sleep` and `now` callables
so each test drives the polling loop through a scripted sequence of
"process state" snapshots without burning wall-clock time or touching the
real `claude` CLI.

The transport choice this module pins down — the `claude ultrareview <PR>`
CLI subcommand documented for CI/script use — is exercised end-to-end via
the default `_default_spawn` only in the dedicated argv-construction test;
every other test stubs the subprocess seam.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from orchestrator import ultrareview

# --------------------------- fakes ---------------------------


class FakePopen:
    """Minimal Popen-shaped stub for `wait_for_result` to poll.

    Walks through a scripted list of `poll()` return values (None = still
    running, int = exit code). When `communicate()` lands we surface the
    pre-canned stdout/stderr.
    """

    def __init__(
        self,
        poll_sequence: list[int | None],
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._poll_sequence = list(poll_sequence)
        self._returncode: int | None = None
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.killed = False
        self.terminated = False
        self.argv: list[str] | None = None

    def poll(self) -> int | None:
        if not self._poll_sequence:
            return self._returncode
        rc = self._poll_sequence.pop(0)
        if rc is not None:
            self._returncode = rc
        return rc

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout_text, self.stderr_text

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    @property
    def returncode(self) -> int | None:
        return self._returncode


class FakeClock:
    """Monotonic clock + sleep recorder. Mirrors test_ci_wait's helper."""

    def __init__(self, step: float = 1.0) -> None:
        self.step = step
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        out = self.t
        self.t += self.step
        return out

    def sleep(self, n: float) -> None:
        self.slept.append(n)


def _scripted_spawn(proc: FakePopen):
    """Return a spawn callable that records argv and returns `proc`."""

    def _spawn(argv: list[str]) -> FakePopen:
        proc.argv = list(argv)
        return proc

    return _spawn


@pytest.fixture(autouse=True)
def _clear_registry():
    """Wipe the module-level run registry between tests."""
    ultrareview._runs.clear()
    yield
    ultrareview._runs.clear()


# --------------------------- pr-url parsing ---------------------------


class TestParsePrUrl:
    def test_canonical_url(self):
        assert ultrareview._parse_pr_url("https://github.com/o/r/pull/42") == ("o", "r", 42)

    def test_trailing_slash(self):
        assert ultrareview._parse_pr_url("https://github.com/o/r/pull/42/") == ("o", "r", 42)

    def test_files_suffix_tolerated(self):
        """Real-world PR URLs sometimes carry /files or /commits suffixes."""
        assert ultrareview._parse_pr_url("https://github.com/o/r/pull/42/files") == ("o", "r", 42)

    def test_rejects_non_pr_url(self):
        with pytest.raises(ValueError):
            ultrareview._parse_pr_url("https://github.com/o/r")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            ultrareview._parse_pr_url("not-a-url")


# --------------------------- trigger() ---------------------------


class TestTrigger:
    def test_spawns_claude_ultrareview_with_pr_number(self):
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert proc.argv is not None
        # Pinned transport: the `claude ultrareview <N>` CLI subcommand,
        # documented at https://code.claude.com/docs/en/ultrareview.
        assert proc.argv[-2:] == ["ultrareview", "7"]
        assert proc.argv[0].endswith("claude")

    def test_registers_run_keyed_by_pr_url(self):
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert "https://github.com/o/r/pull/7" in ultrareview._runs
        assert ultrareview._runs["https://github.com/o/r/pull/7"].process is proc

    def test_re_trigger_replaces_prior_handle(self):
        """Second trigger for same URL supersedes the first (caller error
        recovery — don't keep the stale handle around)."""
        first = FakePopen([None])
        second = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(first))
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(second))
        assert ultrareview._runs["https://github.com/o/r/pull/7"].process is second

    def test_rejects_invalid_pr_url(self):
        with pytest.raises(ValueError):
            ultrareview.trigger("https://github.com/o/r", spawn=_scripted_spawn(FakePopen([])))

    def test_cli_override_env_respected(self, monkeypatch):
        """ULTRAREVIEW_CLI lets users point at a non-PATH binary."""
        monkeypatch.setenv("ULTRAREVIEW_CLI", "/opt/custom/claude")
        import importlib

        importlib.reload(ultrareview)
        try:
            proc = FakePopen([None])
            ultrareview.trigger("https://github.com/o/r/pull/3", spawn=_scripted_spawn(proc))
            assert proc.argv is not None
            assert proc.argv[0] == "/opt/custom/claude"
        finally:
            monkeypatch.delenv("ULTRAREVIEW_CLI", raising=False)
            importlib.reload(ultrareview)


# --------------------------- wait_for_result(): happy path ---------------------------


class TestWaitForResultPass:
    def test_immediate_pass_with_no_findings(self):
        proc = FakePopen([0], stdout="No bugs found.\n")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": ["No bugs found."]}
        assert clock.slept == []

    def test_pending_then_pass(self):
        proc = FakePopen([None, None, 0], stdout="line one\nline two\n")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=1)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is True
        assert r["findings"] == ["line one", "line two"]
        # 2 sleeps before the third poll returned 0
        assert len(clock.slept) == 2

    def test_blank_stdout_yields_empty_findings(self):
        proc = FakePopen([0], stdout="\n   \n")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": []}


# --------------------------- wait_for_result(): failure ---------------------------


class TestWaitForResultFail:
    def test_nonzero_exit_marks_failed(self):
        proc = FakePopen(
            [1],
            stdout="bug: race in auth.py:42\nbug: SQL injection in db.py:99\n",
        )
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False
        assert r["findings"] == [
            "bug: race in auth.py:42",
            "bug: SQL injection in db.py:99",
        ]

    def test_negative_exit_marks_failed(self):
        """Signal-killed subprocess (e.g. SIGTERM) is a fail, not a pass."""
        proc = FakePopen([-9], stdout="")
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False


# --------------------------- wait_for_result(): timeout ---------------------------


class TestWaitForResultTimeout:
    def test_timeout_kills_process_and_returns_failed(self):
        proc = FakePopen([None] * 100)  # never finishes
        ultrareview.trigger("https://github.com/o/r/pull/5", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=5)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/5",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False
        # Findings encode the timeout so callers can surface the reason.
        assert any("timeout" in f.lower() for f in r["findings"])
        # We must hand the process a SIGTERM/SIGKILL so it doesn't outlive
        # the orchestrator — otherwise a stuck `claude` would leak forever.
        assert proc.terminated or proc.killed

    def test_timeout_swallows_kill_errors(self):
        """Stubs whose terminate/kill raise must not crash the timeout path."""

        class BrokenProc(FakePopen):
            def terminate(self) -> None:
                raise OSError("no such process")

            def kill(self) -> None:
                raise PermissionError("denied")

        proc = BrokenProc([None] * 100)
        ultrareview.trigger("https://github.com/o/r/pull/4", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=5)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/4",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False

    def test_timeout_uses_default_when_unset(self, monkeypatch):
        """`timeout=None` falls back to module default."""
        proc = FakePopen([0], stdout="ok")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=None,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is True


# --------------------------- wait_for_result(): missing trigger ---------------------------


class TestWaitForResultMissing:
    def test_no_prior_trigger_raises(self):
        clock = FakeClock(step=0)
        with pytest.raises(RuntimeError, match="no ultrareview run"):
            ultrareview.wait_for_result(
                "https://github.com/o/r/pull/1",
                timeout=10,
                sleep=clock.sleep,
                now=clock.now,
            )


# --------------------------- multiple in-flight runs ---------------------------


class TestParallelRuns:
    def test_multiple_pr_urls_tracked_independently(self):
        a = FakePopen([0], stdout="A: clean\n")
        b = FakePopen([1], stdout="B: bug in core.py\n")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(a))
        ultrareview.trigger("https://github.com/o/r/pull/2", spawn=_scripted_spawn(b))

        clock = FakeClock(step=0)
        ra = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        rb = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/2",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert ra["passed"] is True
        assert rb["passed"] is False
        assert ra["findings"] == ["A: clean"]
        assert rb["findings"] == ["B: bug in core.py"]


# --------------------------- default-spawn argv shape ---------------------------


class TestDefaultSpawnArgv:
    """The one test that drives `_default_spawn` directly — confirms the
    invocation mechanism (CLI subcommand) round-trips through `subprocess`
    with the expected argv. Uses ``sys.executable -c ""`` as the binary
    so the test is portable to every platform where pytest runs (the
    previous ``/bin/true`` choice was absent on Windows runners and the
    macOS GitHub-Actions images).
    """

    def test_default_spawn_returns_popen(self):
        proc = ultrareview._default_spawn([sys.executable, "-c", ""])
        assert isinstance(proc, subprocess.Popen)
        proc.wait(timeout=5)
        assert proc.returncode == 0


# --------------------------- env defaults ---------------------------


def test_env_defaults_override_module_defaults(monkeypatch):
    monkeypatch.setenv("ULTRAREVIEW_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("ULTRAREVIEW_POLL_INTERVAL", "7")

    import importlib

    importlib.reload(ultrareview)
    try:
        assert ultrareview.DEFAULT_TIMEOUT_SECONDS == 42
        assert ultrareview.DEFAULT_POLL_INTERVAL_SECONDS == 7
    finally:
        monkeypatch.delenv("ULTRAREVIEW_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("ULTRAREVIEW_POLL_INTERVAL", raising=False)
        importlib.reload(ultrareview)
