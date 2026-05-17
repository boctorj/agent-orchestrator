"""Tests for orchestrator/ultrareview.py — the `/ultrareview` invocation primitive.

We inject the subprocess `spawn` callable plus `sleep` and `now` callables
so each test drives the polling loop through a scripted sequence of
"process state" snapshots without burning wall-clock time or touching the
real `claude` CLI.

The transport choice this module pins down — the `claude ultrareview <N>
--json --timeout <m>` CLI subcommand documented for CI/script use — is
exercised end-to-end via the default `_default_spawn` only in the
dedicated argv-construction test; every other test stubs the subprocess
seam.

Exit-code semantics (covered here per F-007-U-2 review C1+C2): per
https://code.claude.com/docs/en/ultrareview the CLI exits ``0`` when the
review completes — **with or without bugs** — and ``1`` only when the
review fails to launch / errors / hits its own timeout. The gate
therefore computes ``passed = (rc == 0 AND len(bugs) == 0)``; tests
below pin both the rc==0+bugs and the rc==1+launch-failed cases.
"""

from __future__ import annotations

import json
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
        self.waited = False
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

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return self._returncode or 0

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


def _bugs_json(*entries: dict) -> str:
    """Wrap one or more bug entries in the bugs.json envelope."""
    return json.dumps({"bugs": list(entries)})


@pytest.fixture(autouse=True)
def _clear_registry():
    """Wipe the module-level run registry between tests."""
    ultrareview._runs.clear()
    yield
    ultrareview._runs.clear()


# --------------------------- pr-url canonicalization ---------------------------


class TestCanonicalPrUrl:
    def test_canonical_url(self):
        canonical, n = ultrareview._canonical_pr_url("https://github.com/o/r/pull/42")
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_trailing_slash_normalized(self):
        canonical, n = ultrareview._canonical_pr_url("https://github.com/o/r/pull/42/")
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_files_suffix_normalized(self):
        """Real-world PR URLs sometimes carry /files or /commits suffixes —
        the canonical form must strip them so the registry key is stable."""
        canonical, n = ultrareview._canonical_pr_url("https://github.com/o/r/pull/42/files")
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_rejects_non_pr_url(self):
        with pytest.raises(ValueError):
            ultrareview._canonical_pr_url("https://github.com/o/r")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            ultrareview._canonical_pr_url("not-a-url")

    def test_query_string_normalized(self):
        """GitHub's UI hands users URLs like ``…/pull/42?w=1`` (whitespace
        diff toggle); the canonicalizer must strip the query string."""
        canonical, n = ultrareview._canonical_pr_url("https://github.com/o/r/pull/42?w=1")
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_fragment_normalized(self):
        """``…/pull/42#issuecomment-1234`` is the form deep-linking a
        specific comment hands you."""
        canonical, n = ultrareview._canonical_pr_url(
            "https://github.com/o/r/pull/42#issuecomment-1234"
        )
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42

    def test_path_then_query_normalized(self):
        canonical, n = ultrareview._canonical_pr_url(
            "https://github.com/o/r/pull/42/files?diff=split"
        )
        assert canonical == "https://github.com/o/r/pull/42"
        assert n == 42


# --------------------------- trigger() ---------------------------


class TestTrigger:
    def test_spawns_claude_ultrareview_with_pr_number(self):
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert proc.argv is not None
        # Pinned transport: the `claude ultrareview <N> --json --timeout <m>`
        # CLI subcommand, documented at
        # https://code.claude.com/docs/en/ultrareview.
        assert proc.argv[0].endswith("claude")
        assert proc.argv[1] == "ultrareview"
        assert proc.argv[2] == "7"
        assert "--json" in proc.argv
        assert "--timeout" in proc.argv

    def test_argv_includes_json_flag(self):
        """--json switches the CLI to emit raw bugs.json instead of a
        human-formatted report — the gate needs structured data."""
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert proc.argv is not None
        assert "--json" in proc.argv

    def test_argv_includes_timeout_aligned_with_wrapper(self):
        """The CLI --timeout matches the wrapper-side cap so an early
        wrapper SIGKILL also stops the cloud session (and stops billing).
        Default = DEFAULT_TIMEOUT_SECONDS / 60."""
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert proc.argv is not None
        idx = proc.argv.index("--timeout")
        # The value is in minutes; default 1800s → 30 min.
        assert int(proc.argv[idx + 1]) == ultrareview.DEFAULT_TIMEOUT_SECONDS // 60

    def test_registers_run_keyed_by_canonical_url(self):
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert "https://github.com/o/r/pull/7" in ultrareview._runs
        assert ultrareview._runs["https://github.com/o/r/pull/7"].process is proc

    def test_trigger_and_wait_canonicalize_url_consistently(self):
        """Trigger with one spelling, wait with another — same PR, same run.
        Pins the Copilot-flagged consistency between the two callsites."""
        proc = FakePopen([0], stdout=_bugs_json())
        ultrareview.trigger("https://github.com/o/r/pull/7/files", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/7/",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is True

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

    def test_caller_supplied_timeout_forwarded_to_cli(self):
        """Per M2: a caller-tightened ``timeout_seconds`` must flow into
        the CLI's ``--timeout`` so wrapper-side SIGKILL and cloud-side
        cap fire together (no billing leak)."""
        proc = FakePopen([None])
        ultrareview.trigger(
            "https://github.com/o/r/pull/7",
            timeout_seconds=120,  # 2 minutes
            spawn=_scripted_spawn(proc),
        )
        assert proc.argv is not None
        idx = proc.argv.index("--timeout")
        assert int(proc.argv[idx + 1]) == 2

    def test_caller_supplied_sub_minute_timeout_rounds_up(self):
        """Sub-minute caller timeouts still produce a valid ``--timeout``;
        rounding up means the CLI doesn't kill earlier than the wrapper."""
        proc = FakePopen([None])
        ultrareview.trigger(
            "https://github.com/o/r/pull/7",
            timeout_seconds=30,
            spawn=_scripted_spawn(proc),
        )
        assert proc.argv is not None
        idx = proc.argv.index("--timeout")
        assert int(proc.argv[idx + 1]) == 1

    def test_default_trigger_timeout_uses_module_default(self):
        """Caller omits ``timeout_seconds`` → trigger uses
        ``DEFAULT_TIMEOUT_SECONDS`` (the CLI's documented 30-min default).
        Distinct from wait_for_result's 600s spec default."""
        proc = FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_scripted_spawn(proc))
        assert proc.argv is not None
        idx = proc.argv.index("--timeout")
        assert int(proc.argv[idx + 1]) == ultrareview.DEFAULT_TIMEOUT_SECONDS // 60


# --------------------------- wait_for_result(): passed ---------------------------


class TestWaitForResultPass:
    def test_immediate_pass_with_empty_bugs(self):
        """Clean review: rc==0 + empty bugs list → passed=True, no findings."""
        proc = FakePopen([0], stdout=_bugs_json())
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": []}
        assert clock.slept == []

    def test_pending_then_pass(self):
        proc = FakePopen([None, None, 0], stdout=_bugs_json())
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=1)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/1",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": []}
        # 2 sleeps before the third poll returned 0
        assert len(clock.slept) == 2

    def test_blank_stdout_yields_empty_findings(self):
        """Empty stdout on rc==0 is treated as a clean review (no bugs.json
        emitted at all → no bugs reported). Defensive against CLI changes."""
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


# --------------------------- wait_for_result(): bugs-found path ---------------------------


class TestWaitForResultBugsFound:
    """The case the F-007 gate exists to catch: review completed (rc==0)
    AND bugs.json is non-empty. Reviewer C1+C2 flagged the previous
    implementation as a no-op for exactly this case."""

    def test_rc0_with_bugs_marks_failed(self):
        bugs = _bugs_json(
            {"path": "auth.py", "line": 42, "summary": "race in token refresh"},
            {"path": "db.py", "line": 99, "summary": "unparameterized SQL"},
        )
        proc = FakePopen([0], stdout=bugs)
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False, (
            "rc==0 with non-empty bugs.json is the bug-found-but-review-completed "
            "case — gate MUST block ready-to-merge here"
        )
        assert len(r["findings"]) == 2
        assert "auth.py:42 — race in token refresh" in r["findings"]
        assert "db.py:99 — unparameterized SQL" in r["findings"]

    def test_top_level_list_bugs_payload(self):
        """The bugs.json schema may also surface as a top-level list."""
        bugs = json.dumps([{"path": "x.py", "line": 1, "summary": "bug"}])
        proc = FakePopen([0], stdout=bugs)
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
        assert r["findings"] == ["x.py:1 — bug"]

    def test_bug_with_alternative_summary_field(self):
        bugs = _bugs_json({"path": "a.py", "line": 3, "message": "alt-field"})
        proc = FakePopen([0], stdout=bugs)
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["findings"] == ["a.py:3 — alt-field"]

    def test_non_dict_bug_entry_renders_via_str(self):
        """Defensive: a bug entry that's a bare string (unexpected
        schema) shouldn't crash the renderer."""
        bugs = json.dumps({"bugs": ["legacy bare string"]})
        proc = FakePopen([0], stdout=bugs)
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=60,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["findings"] == ["legacy bare string"]
        assert r["passed"] is False

    def test_top_level_json_neither_dict_nor_list(self):
        """JSON that parses to a primitive (number / bare string) is
        surfaced as a single finding so the operator can debug."""
        proc = FakePopen([0], stdout='"unexpected-shape"')
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
        assert len(r["findings"]) == 1


# --------------------------- wait_for_result(): launch-failed (rc != 0) ---------------------------


class TestWaitForResultLaunchFailed:
    """Per the docs, rc != 0 means the review never produced a verdict —
    launch failure, remote session error, or CLI-side timeout. Distinct
    semantic from "bugs found"; both end up passed=False but the gate's
    user-facing message should be able to tell them apart."""

    def test_rc1_with_empty_stdout(self):
        proc = FakePopen([1], stdout="")
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
        assert r["findings"] == []

    def test_rc1_with_error_text_surfaces_as_finding(self):
        """If the CLI prints an error banner to stdout, we surface it so
        the operator sees what happened (parseable JSON would have been
        treated as bugs; non-JSON falls back to a single finding entry)."""
        proc = FakePopen([1], stdout="ERROR: remote session crashed (id abc123)")
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
        assert any("crashed" in f for f in r["findings"])

    def test_rc130_signal_marks_failed(self):
        """Per docs, code 130 = SIGINT (Ctrl-C). Same family as rc==1
        from the gate's POV: review never produced a verdict."""
        proc = FakePopen([130], stdout="")
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
    def test_timeout_kills_and_reaps_process(self):
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
        # SIGTERM + SIGKILL + wait() — the reap step prevents a zombie.
        assert proc.terminated or proc.killed
        assert proc.waited, "_kill_and_reap must wait() after kill to reap zombies"

    def test_timeout_swallows_kill_errors(self):
        """Stubs whose terminate/kill/wait raise must not crash the timeout
        path."""

        class BrokenProc(FakePopen):
            def terminate(self) -> None:
                raise OSError("no such process")

            def kill(self) -> None:
                raise PermissionError("denied")

            def wait(self, timeout: float | None = None) -> int:
                raise OSError("dead")

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

    def test_timeout_uses_default_when_unset(self):
        """`timeout=None` falls back to module default."""
        proc = FakePopen([0], stdout=_bugs_json())
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
        a = FakePopen([0], stdout=_bugs_json())
        b = FakePopen([0], stdout=_bugs_json({"path": "core.py", "line": 1, "summary": "B"}))
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
        assert ra["findings"] == []
        assert rb["findings"] == ["core.py:1 — B"]


# --------------------------- fail-closed: schema drift / unparseable JSON ---------------------------


class TestFailClosedOnRcZero:
    """Per reviewer H1 (this round), the safety gate must fail *closed*
    on rc==0 when stdout is unparseable or schema-drifted. Endorsing
    a merge based on output we couldn't read is exactly the case this
    module exists to prevent."""

    def test_rc0_with_non_json_stdout_blocks_merge(self):
        """A CLI error banner / non-JSON dump on rc==0 must surface as
        a sentinel finding so ``passed`` flips to False."""
        proc = FakePopen([0], stdout="ERROR: CLI failed to emit bugs.json\n")
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False, (
            "rc==0 + unparseable stdout must NOT be treated as 'no bugs' — "
            "the gate has to fail closed, not silently endorse"
        )
        assert len(r["findings"]) == 1
        assert "not parseable" in r["findings"][0]
        assert "ERROR" in r["findings"][0]

    def test_rc0_with_schema_drift_blocks_merge(self):
        """If the bugs.json schema renames ``bugs`` to ``findings`` /
        ``results`` / etc., the gate must NOT treat the missing key as
        ``len(bugs) == 0``."""
        drifted = json.dumps({"findings": [{"path": "x.py", "line": 1, "summary": "hidden bug"}]})
        proc = FakePopen([0], stdout=drifted)
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False, (
            "rc==0 + JSON dict missing 'bugs' key must NOT silently flip "
            "passed=True; surface the schema mismatch so it can be debugged"
        )
        assert len(r["findings"]) == 1
        assert "schema unrecognized" in r["findings"][0]
        assert "findings" in r["findings"][0]  # the actual key found

    def test_rc0_with_huge_non_json_truncates(self):
        """A non-JSON dump 10x the size of the snippet cap doesn't
        balloon the surfaced finding."""
        huge = "X" * 5000
        proc = FakePopen([0], stdout=huge)
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False
        # One finding entry; truncated with an ellipsis marker.
        assert len(r["findings"]) == 1
        assert "…" in r["findings"][0]
        assert len(r["findings"][0]) < 1000

    def test_rc0_empty_stdout_still_passes(self):
        """Empty stdout is the ONE 'trust the completion bit' path —
        the CLI may not emit bugs.json at all on a clean review."""
        proc = FakePopen([0], stdout="")
        ultrareview.trigger("https://github.com/o/r/pull/9", spawn=_scripted_spawn(proc))
        clock = FakeClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/9",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": []}


# --------------------------- default-spawn argv shape ---------------------------


class TestDefaultSpawnArgv:
    """Smoke-check `_default_spawn` against a real subprocess.

    Every other test stubs the spawn seam; this one drives the real
    :func:`subprocess.Popen` so we catch regressions in the argv /
    kwargs shape (PIPE for stdout, DEVNULL for stderr, text mode).
    Uses ``sys.executable -c ""`` rather than a hardcoded binary so it
    works on Linux, macOS, and Windows runners.
    """

    def test_default_spawn_returns_popen(self):
        proc = ultrareview._default_spawn([sys.executable, "-c", ""])
        try:
            assert isinstance(proc, subprocess.Popen)
            proc.wait(timeout=10)
            assert proc.returncode == 0
        finally:
            # Drain pipes so the OS reclaims the fds even if asserts fail.
            proc.communicate()

    def test_default_spawn_uses_devnull_for_stderr(self, monkeypatch):
        """stderr=DEVNULL prevents the pipe-buffer deadlock on long runs —
        Copilot flagged that progress chatter over 5-30 min could exceed
        the OS pipe buffer if we PIPE+don't-drain stderr."""
        captured: dict = {}

        class _PopenSpy:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs

        monkeypatch.setattr(ultrareview.subprocess, "Popen", _PopenSpy)
        ultrareview._default_spawn(["claude", "ultrareview", "1"])
        assert captured["kwargs"].get("stderr") == subprocess.DEVNULL
        # stdout still PIPE'd — bugs.json payload is bounded JSON.
        assert captured["kwargs"].get("stdout") == subprocess.PIPE


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


def test_default_timeout_matches_cli_default():
    """Per the canonical docs (https://code.claude.com/docs/en/ultrareview)
    the CLI's own --timeout defaults to 30 minutes. The wrapper aligns
    with that — a smaller wrapper default produces false-negative timeouts
    + cloud-side billing leaks (reviewer H1)."""
    import importlib

    # Reload with env knob unset to test the hardcoded fallback.
    import os

    saved = os.environ.pop("ULTRAREVIEW_TIMEOUT_SECONDS", None)
    try:
        importlib.reload(ultrareview)
        assert ultrareview.DEFAULT_TIMEOUT_SECONDS == 1800
    finally:
        if saved is not None:
            os.environ["ULTRAREVIEW_TIMEOUT_SECONDS"] = saved
        importlib.reload(ultrareview)
