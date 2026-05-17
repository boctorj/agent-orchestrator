"""Tester-written contracts for F-007-U-2 (ultrareview invocation primitive).

These tests are independent of the coder's own ``tests/test_ultrareview.py``.
They lock subtle behaviour the existing suite doesn't fully pin:

  * The ``wait_for_result`` signature literally defaults ``timeout`` to
    ``600`` (matches the unit description verbatim — if env-driven
    overrides ever change the *signature* default, the spec contract
    silently drifts).
  * The return-shape contract is exact — exactly the keys ``passed`` and
    ``findings``, with ``passed`` a real :class:`bool` and ``findings``
    a real :class:`list` of :class:`str` (no tuples, no generators, no
    extra ``stderr``/``elapsed`` leakage). Callers route on these keys;
    a stray extra key would either fail a strict consumer or worse,
    silently expose internal state.
  * ``trigger`` is genuinely non-blocking — it never calls ``communicate``
    / ``wait`` / ``poll`` on the returned process. A subprocess wrapper
    that secretly blocks would defeat the whole "fire-and-forget" split
    F-007 needs.
  * ``trigger`` validates the URL *before* shelling out — no subprocess
    is ever spawned on a bad input. A spawn-then-fail ordering would
    leak `claude` processes on every malformed call.
  * The invocation mechanism is the documented CLI subcommand, not the
    interactive slash command nor a PR-comment trigger. The module
    docstring must record that decision so a future maintainer doesn't
    flip transports without re-reading the rationale.
  * ``_parse_findings`` handles CRLF line endings (Windows runners' CLI
    output) and preserves the order findings appear in stdout.
  * stderr is NEVER folded into ``findings`` — Anthropic's docs say
    progress chatter and the live-session URL go to stderr; if we
    surfaced those as "findings" we'd noise up the PR-comment digest.
  * ``_default_spawn`` does NOT pass ``shell=True`` — argv-list form
    means a malicious PR URL has no shell-injection surface.
  * The public ``__all__`` exports both spec-named functions
    (``trigger`` + ``wait_for_result``); a typo there would silently
    break ``from orchestrator.ultrareview import *`` consumers.
  * Idempotent re-poll: calling ``wait_for_result`` a second time on a
    URL whose run already finished returns the same dict — the handle
    is not consumed, so retry-on-network-blip is safe.
  * ``None`` stdout from a stubbed ``communicate()`` is tolerated (the
    real Popen returns ``None`` on a stream that was never opened).

All subprocess interactions are injected via the ``spawn``/``sleep``/``now``
hooks; no live network, no real ``claude`` CLI, no wall-clock waits.
"""

from __future__ import annotations

import inspect
import subprocess
from typing import Any

import pytest

from orchestrator import ultrareview

# --------------------------- shared fakes ---------------------------


class _FakePopen:
    """Popen-shaped stub.

    Distinct from the coder's ``FakePopen`` so the two suites are
    genuinely independent — same protocol, separate implementation.
    Records every method call so non-blocking / drain-pipe assertions
    have something to check.
    """

    def __init__(
        self,
        poll_sequence: list[int | None] | None = None,
        stdout: str | None = "",
        stderr: str | None = "",
    ) -> None:
        self._poll_sequence: list[int | None] = list(poll_sequence or [None])
        self._final_rc: int | None = None
        self._stdout = stdout
        self._stderr = stderr
        self.calls: list[str] = []
        self.argv: list[str] | None = None

    def poll(self) -> int | None:
        self.calls.append("poll")
        if not self._poll_sequence:
            return self._final_rc
        rc = self._poll_sequence.pop(0)
        if rc is not None:
            self._final_rc = rc
        return rc

    def communicate(self, timeout: float | None = None) -> tuple[str | None, str | None]:
        self.calls.append("communicate")
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.calls.append("terminate")
        self._final_rc = -15

    def kill(self) -> None:
        self.calls.append("kill")
        self._final_rc = -9

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append("wait")
        return self._final_rc or 0

    @property
    def returncode(self) -> int | None:
        return self._final_rc


class _StepClock:
    """Deterministic monotonic clock + sleep recorder."""

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


def _record_spawn(proc: _FakePopen):
    """Build a spawn callable that captures argv and returns *proc*."""

    def _spawn(argv: list[str]) -> _FakePopen:
        proc.argv = list(argv)
        return proc

    return _spawn


@pytest.fixture(autouse=True)
def _clear_runs():
    """Module-level registry must not leak across tests."""
    ultrareview._runs.clear()
    yield
    ultrareview._runs.clear()


# ---------------------------------------------------------------------------
# Signature defaults — match the unit description literally.
# ---------------------------------------------------------------------------


class TestSignatureMatchesSpec:
    """The unit description names ``wait_for_result(pr_url, timeout=600)``
    as the function signature. Even with env-driven runtime overrides,
    the *signature default* must read 600 — otherwise a fresh reader of
    the source can't tell what the documented contract is."""

    def test_wait_for_result_default_timeout_is_600(self, monkeypatch) -> None:
        # Snapshot the signature against a freshly-reloaded module with
        # the env knob explicitly unset, so a developer who happens to
        # have ULTRAREVIEW_TIMEOUT_SECONDS set in their shell doesn't
        # see a green-on-laptop / red-in-CI mismatch.
        monkeypatch.delenv("ULTRAREVIEW_TIMEOUT_SECONDS", raising=False)
        import importlib

        importlib.reload(ultrareview)
        try:
            sig = inspect.signature(ultrareview.wait_for_result)
            default = sig.parameters["timeout"].default
            assert default == 600, (
                f"unit description names timeout=600 as the documented "
                f"default; signature reads timeout={default!r}"
            )
        finally:
            importlib.reload(ultrareview)

    def test_default_timeout_constant_is_600_without_env(self, monkeypatch) -> None:
        monkeypatch.delenv("ULTRAREVIEW_TIMEOUT_SECONDS", raising=False)
        import importlib

        importlib.reload(ultrareview)
        try:
            assert ultrareview.DEFAULT_TIMEOUT_SECONDS == 600
        finally:
            importlib.reload(ultrareview)

    def test_trigger_takes_one_positional_pr_url(self) -> None:
        """``trigger(pr_url)`` per the unit description — one required
        positional. A second required positional would be a breaking
        API drift."""
        sig = inspect.signature(ultrareview.trigger)
        params = list(sig.parameters.values())
        required = [
            p
            for p in params
            if p.default is inspect.Parameter.empty
            and p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        assert len(required) == 1, (
            f"trigger must take exactly one required positional (pr_url); got {required!r}"
        )
        assert required[0].name == "pr_url"

    def test_default_poll_interval_is_positive(self) -> None:
        """Poll interval must be > 0; a 0 default would spin the CPU."""
        assert ultrareview.DEFAULT_POLL_INTERVAL_SECONDS > 0

    def test_public_all_exports_spec_names(self) -> None:
        """`from orchestrator.ultrareview import *` must export both
        spec-named functions."""
        assert "trigger" in ultrareview.__all__
        assert "wait_for_result" in ultrareview.__all__


# ---------------------------------------------------------------------------
# Return-shape contract — happy path, failure path, timeout path all share
# the same `{passed: bool, findings: list[str]}` shape with no extras.
# ---------------------------------------------------------------------------


class TestReturnShape:
    PR = "https://github.com/o/r/pull/1"

    def _wait(self, proc: _FakePopen, **kw: Any) -> dict[str, Any]:
        ultrareview.trigger(self.PR, spawn=_record_spawn(proc))
        clock = _StepClock(step=kw.pop("step", 0))
        return ultrareview.wait_for_result(
            self.PR,
            timeout=kw.pop("timeout", 10),
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )

    def test_pass_shape_is_exact(self) -> None:
        r = self._wait(_FakePopen([0], stdout="ok\n"))
        assert set(r.keys()) == {"passed", "findings"}, (
            f"return dict must have exactly two keys; got {sorted(r.keys())!r}"
        )

    def test_fail_shape_is_exact(self) -> None:
        r = self._wait(_FakePopen([1], stdout="bug\n"))
        assert set(r.keys()) == {"passed", "findings"}

    def test_timeout_shape_is_exact(self) -> None:
        r = self._wait(_FakePopen([None] * 100), step=5, timeout=10)
        assert set(r.keys()) == {"passed", "findings"}

    def test_passed_is_real_bool_on_pass(self) -> None:
        r = self._wait(_FakePopen([0], stdout="ok"))
        # `bool` is a subclass of `int`; assertion must be strict.
        assert r["passed"] is True
        assert type(r["passed"]) is bool, (
            f"`passed` must be a Python bool, not a bool-ish int; got {type(r['passed'])!r}"
        )

    def test_passed_is_real_bool_on_fail(self) -> None:
        r = self._wait(_FakePopen([1], stdout=""))
        assert r["passed"] is False
        assert type(r["passed"]) is bool

    def test_findings_is_real_list_not_tuple(self) -> None:
        r = self._wait(_FakePopen([0], stdout="a\nb\n"))
        assert type(r["findings"]) is list, (
            f"`findings` must be a list (callers mutate / append); got {type(r['findings'])!r}"
        )

    def test_findings_every_element_is_str(self) -> None:
        r = self._wait(_FakePopen([1], stdout="line1\nline2\n"))
        assert all(isinstance(f, str) for f in r["findings"])

    def test_findings_empty_list_on_blank_stdout(self) -> None:
        r = self._wait(_FakePopen([0], stdout=""))
        assert r["findings"] == []

    def test_no_stderr_key_leaked(self) -> None:
        """The module captures stderr internally but must NOT surface it
        as a public key. Adding it later is a non-breaking change; having
        it accidentally already is a confusing public contract."""
        r = self._wait(_FakePopen([0], stdout="ok", stderr="progress: 12%\n"))
        assert "stderr" not in r
        assert "elapsed" not in r
        assert "returncode" not in r


# ---------------------------------------------------------------------------
# `trigger` is non-blocking: it must NOT call any waiting / draining method
# on the subprocess. A wrapper that secretly called `.wait()` or
# `.communicate()` would block the orchestrator's hot path for the full
# 5-10 minute ultrareview run.
# ---------------------------------------------------------------------------


class TestTriggerNonBlocking:
    def test_trigger_does_not_call_communicate(self) -> None:
        proc = _FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_record_spawn(proc))
        assert "communicate" not in proc.calls, (
            f"trigger must be non-blocking; called {proc.calls!r}"
        )

    def test_trigger_does_not_call_wait(self) -> None:
        proc = _FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_record_spawn(proc))
        assert "wait" not in proc.calls

    def test_trigger_does_not_call_poll(self) -> None:
        """`poll()` itself doesn't block — but a `trigger` that called
        it speculatively is a smell (suggests a synchronous design that
        regressed). Lock the fire-and-forget contract."""
        proc = _FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_record_spawn(proc))
        assert "poll" not in proc.calls

    def test_trigger_returns_none(self) -> None:
        """Spec says trigger returns None (fire-and-forget)."""
        proc = _FakePopen([None])
        result = ultrareview.trigger("https://github.com/o/r/pull/7", spawn=_record_spawn(proc))
        assert result is None


# ---------------------------------------------------------------------------
# Trigger validates URL BEFORE spawning. A spawn-then-validate ordering
# would leak `claude` processes on every malformed PR URL.
# ---------------------------------------------------------------------------


class TestTriggerValidatesBeforeSpawn:
    def test_bad_url_does_not_invoke_spawn_callable(self) -> None:
        called: list[list[str]] = []

        def _spy_spawn(argv: list[str]) -> _FakePopen:
            called.append(list(argv))
            return _FakePopen([0])

        with pytest.raises(ValueError):
            ultrareview.trigger("not-a-pr-url", spawn=_spy_spawn)
        assert called == [], (
            f"trigger must validate before shelling out; spawn was invoked with {called!r}"
        )

    def test_bad_url_does_not_register_handle(self) -> None:
        with pytest.raises(ValueError):
            ultrareview.trigger(
                "https://github.com/o/r/issues/1", spawn=_record_spawn(_FakePopen([0]))
            )
        assert ultrareview._runs == {}, "trigger must not register a handle for an unparseable URL"


# ---------------------------------------------------------------------------
# Invocation mechanism: documented CLI subcommand, NOT the interactive slash
# command nor a PR-comment trigger. Lock the design decision in the module
# docstring so a future maintainer can't silently flip the transport.
# ---------------------------------------------------------------------------


class TestInvocationMechanismDocumented:
    def test_module_docstring_records_choice(self) -> None:
        doc = ultrareview.__doc__ or ""
        # The decision is "CLI subcommand". The docstring must say so —
        # not just "claude" or "subprocess", but actually name CLI.
        assert "CLI" in doc, (
            "module docstring must record that the pinned transport is the CLI "
            f"subcommand; current docstring:\n{doc!r}"
        )

    def test_module_docstring_mentions_alternative_transports(self) -> None:
        """The unit description tells the coder to *research* the
        alternatives — slash command, CLI, PR-comment — and pin one.
        Pinning is only credible if the alternatives are visible in the
        same docstring."""
        doc = (ultrareview.__doc__ or "").lower()
        for marker in ("slash", "cli", "comment"):
            assert marker in doc, (
                f"module docstring must enumerate the considered transports; "
                f"missing reference to {marker!r}"
            )

    def test_argv_uses_subcommand_not_slash_form(self) -> None:
        """A regression to ``claude '/ultrareview <N>'`` (single shell-
        quoted slash-command argument) is the most plausible
        wrong-transport drift. Argv must be three explicit tokens."""
        proc = _FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/3", spawn=_record_spawn(proc))
        assert proc.argv is not None
        assert "/ultrareview" not in proc.argv, (
            f"argv must use the CLI subcommand form, not the slash-command "
            f"prefix; got argv={proc.argv!r}"
        )
        # The subcommand token must be exactly "ultrareview" — not
        # "/ultrareview", not "ultra-review", not "ultraReview".
        assert "ultrareview" in proc.argv


# ---------------------------------------------------------------------------
# `_default_spawn` must NOT use shell=True. argv-list form means a PR URL
# loaded via API can't smuggle a `; rm -rf /` past the subprocess seam.
# ---------------------------------------------------------------------------


class TestDefaultSpawnNoShell:
    def test_default_spawn_uses_argv_list_not_shell(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        class _PopenSpy:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["args"] = args
                captured["kwargs"] = kwargs

            def poll(self) -> int | None:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                return "", ""

        monkeypatch.setattr(ultrareview.subprocess, "Popen", _PopenSpy)
        ultrareview._default_spawn(["claude", "ultrareview", "1"])
        # Either explicitly shell=False, or the kwarg absent (the
        # subprocess default is False, but explicit absence still means
        # no shell interpolation happened).
        assert captured["kwargs"].get("shell", False) is False
        # First positional must be an argv list, not a string.
        assert isinstance(captured["args"][0], list)


# ---------------------------------------------------------------------------
# Findings parsing: order preserved, CRLF tolerated, stderr never folded in.
# ---------------------------------------------------------------------------


class TestFindingsParsing:
    PR = "https://github.com/o/r/pull/2"

    def _wait(self, stdout: str, stderr: str = "") -> dict[str, Any]:
        proc = _FakePopen([0], stdout=stdout, stderr=stderr)
        ultrareview.trigger(self.PR, spawn=_record_spawn(proc))
        clock = _StepClock(step=0)
        return ultrareview.wait_for_result(
            self.PR,
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )

    def test_order_is_preserved(self) -> None:
        r = self._wait("first finding\nsecond finding\nthird finding\n")
        assert r["findings"] == ["first finding", "second finding", "third finding"]

    def test_crlf_line_endings(self) -> None:
        """Windows runners' subprocess output uses \\r\\n; `splitlines()`
        handles it but the strip step must clean up any residual \\r."""
        r = self._wait("alpha\r\nbeta\r\ngamma\r\n")
        assert r["findings"] == ["alpha", "beta", "gamma"], (
            f"CRLF line endings must be normalised; got {r['findings']!r}"
        )

    def test_stderr_never_folded_into_findings(self) -> None:
        """Anthropic's CLI sends progress chatter and the live-session
        URL to stderr; only stdout carries the finding payload. A
        regression that concatenates stderr would pollute the digest."""
        r = self._wait(
            stdout="actual finding\n",
            stderr="https://claude.ai/sessions/abc\nprogress: 50%\n",
        )
        assert r["findings"] == ["actual finding"]
        for finding in r["findings"]:
            assert "progress" not in finding
            assert "claude.ai/sessions" not in finding

    def test_lone_whitespace_lines_dropped(self) -> None:
        r = self._wait("real\n   \n\t\nalso-real\n")
        assert r["findings"] == ["real", "also-real"]

    def test_leading_trailing_whitespace_stripped_per_line(self) -> None:
        r = self._wait("  indented finding  \n\treal\t\n")
        assert r["findings"] == ["indented finding", "real"]


# ---------------------------------------------------------------------------
# Tolerance for `None` stdout from a `communicate()` stub. Real Popen with
# `stdout=PIPE` returns a string; a stream that wasn't piped (or a stubbed
# process whose stdout was never set) returns None. The implementation
# guards via `stdout or ""`; lock the guard so a future refactor that
# drops it surfaces here.
# ---------------------------------------------------------------------------


class TestNoneStdoutTolerated:
    def test_none_stdout_yields_empty_findings(self) -> None:
        proc = _FakePopen([0], stdout=None)
        ultrareview.trigger("https://github.com/o/r/pull/8", spawn=_record_spawn(proc))
        clock = _StepClock(step=0)
        r = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/8",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r == {"passed": True, "findings": []}


# ---------------------------------------------------------------------------
# Idempotent re-poll: once a run has finished, a second `wait_for_result`
# call returns the same dict. The handle isn't consumed.
#
# This isn't a hard spec requirement, but it IS the contract the cycle_review
# wiring will lean on if it ever needs to re-harvest the verdict after a
# transient hiccup (e.g. ntfy push failed and we want to retry). Pin it so
# the design space stays open.
# ---------------------------------------------------------------------------


class TestIdempotentRePoll:
    def test_second_wait_returns_same_result(self) -> None:
        proc = _FakePopen([0], stdout="findings here\n")
        ultrareview.trigger("https://github.com/o/r/pull/11", spawn=_record_spawn(proc))
        clock = _StepClock(step=0)
        first = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/11",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        # Re-call with a *fresh* clock to prove there's no time-dependence.
        clock2 = _StepClock(step=0)
        second = ultrareview.wait_for_result(
            "https://github.com/o/r/pull/11",
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock2.sleep,
            now=clock2.now,
        )
        assert first == second
        # The handle should still be in the registry — no auto-removal.
        assert "https://github.com/o/r/pull/11" in ultrareview._runs


# ---------------------------------------------------------------------------
# Timeout path: the process MUST receive a terminate or kill signal, AND
# the result must encode the timeout duration so an operator can recognise
# it in the surfaced PR comment.
# ---------------------------------------------------------------------------


class TestTimeoutPath:
    PR = "https://github.com/o/r/pull/5"

    def test_timeout_invokes_terminate_then_kill(self) -> None:
        """The implementation does `terminate()` then `kill()` — at least
        one must land. A stuck cloud-side review that ignores SIGTERM
        still has to receive SIGKILL eventually."""
        proc = _FakePopen([None] * 50)
        ultrareview.trigger(self.PR, spawn=_record_spawn(proc))
        clock = _StepClock(step=5)
        ultrareview.wait_for_result(
            self.PR,
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert "terminate" in proc.calls or "kill" in proc.calls, (
            f"timeout path must signal the subprocess; calls={proc.calls!r}"
        )

    def test_timeout_findings_encode_duration(self) -> None:
        proc = _FakePopen([None] * 50)
        ultrareview.trigger(self.PR, spawn=_record_spawn(proc))
        clock = _StepClock(step=5)
        r = ultrareview.wait_for_result(
            self.PR,
            timeout=10,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False
        assert any("10" in f for f in r["findings"]), (
            f"timeout findings must surface the configured timeout (10s); got {r['findings']!r}"
        )

    def test_timeout_zero_returns_immediately_if_not_done(self) -> None:
        """A timeout of 0 is an edge case worth pinning: the loop must
        not race past the timeout check on the first iteration."""
        proc = _FakePopen([None] * 50)
        ultrareview.trigger(self.PR, spawn=_record_spawn(proc))
        clock = _StepClock(step=1)
        r = ultrareview.wait_for_result(
            self.PR,
            timeout=0,
            poll_interval_seconds=1,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r["passed"] is False


# ---------------------------------------------------------------------------
# Concurrent runs on different PRs use independent processes — no
# cross-contamination via the module-level registry.
# ---------------------------------------------------------------------------


class TestRegistryIsolation:
    def test_separate_pr_urls_use_separate_processes(self) -> None:
        proc_a = _FakePopen([0], stdout="A clean\n")
        proc_b = _FakePopen([1], stdout="B bug\n")
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_record_spawn(proc_a))
        ultrareview.trigger("https://github.com/o/r/pull/2", spawn=_record_spawn(proc_b))

        handle_a = ultrareview._runs["https://github.com/o/r/pull/1"]
        handle_b = ultrareview._runs["https://github.com/o/r/pull/2"]
        assert handle_a.process is proc_a
        assert handle_b.process is proc_b
        assert handle_a.process is not handle_b.process

    def test_clearing_one_url_leaves_other_intact(self) -> None:
        """Distinct keys hash to distinct slots; deleting one entry must
        not disturb another. (Sanity guard against a future refactor
        that switches the registry to something non-dict-shaped.)"""
        proc_a = _FakePopen([0])
        proc_b = _FakePopen([0])
        ultrareview.trigger("https://github.com/o/r/pull/1", spawn=_record_spawn(proc_a))
        ultrareview.trigger("https://github.com/o/r/pull/2", spawn=_record_spawn(proc_b))
        del ultrareview._runs["https://github.com/o/r/pull/1"]
        assert "https://github.com/o/r/pull/2" in ultrareview._runs


# ---------------------------------------------------------------------------
# `wait_for_result` raises a *specific* RuntimeError (not generic Exception)
# when no prior trigger was issued; callers may switch on the type.
# ---------------------------------------------------------------------------


class TestMissingTriggerError:
    def test_raises_runtime_error_specifically(self) -> None:
        with pytest.raises(RuntimeError):
            ultrareview.wait_for_result("https://github.com/o/r/pull/9", timeout=1)

    def test_error_message_names_the_url(self) -> None:
        try:
            ultrareview.wait_for_result("https://github.com/o/r/pull/9", timeout=1)
        except RuntimeError as exc:
            assert "https://github.com/o/r/pull/9" in str(exc), (
                f"missing-trigger error must name the offending URL; got: {exc!r}"
            )
        else:
            pytest.fail("expected RuntimeError")


# ---------------------------------------------------------------------------
# argv shape: the PR number is rendered as a string (Popen requires str
# elements). A `[..., 7]` (int) argv crashes on the real subprocess seam
# with TypeError. Lock the str conversion.
# ---------------------------------------------------------------------------


class TestArgvElementsAreStrings:
    def test_pr_number_is_stringified(self) -> None:
        proc = _FakePopen([None])
        ultrareview.trigger("https://github.com/o/r/pull/42", spawn=_record_spawn(proc))
        assert proc.argv is not None
        assert all(isinstance(tok, str) for tok in proc.argv), (
            f"argv elements must all be strings; got {proc.argv!r}"
        )
        assert proc.argv[-1] == "42"


# ---------------------------------------------------------------------------
# `_default_spawn` shape sanity: stdout/stderr ARE PIPE'd (so
# `communicate()` can collect them), text=True (so findings are decoded
# strings, not bytes).
# ---------------------------------------------------------------------------


class TestDefaultSpawnPipeShape:
    def test_pipes_stdout_and_stderr(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        class _PopenSpy:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["args"] = args
                captured["kwargs"] = kwargs

        monkeypatch.setattr(ultrareview.subprocess, "Popen", _PopenSpy)
        ultrareview._default_spawn(["claude", "ultrareview", "1"])
        assert captured["kwargs"].get("stdout") == subprocess.PIPE
        assert captured["kwargs"].get("stderr") == subprocess.PIPE

    def test_text_mode_for_string_findings(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        class _PopenSpy:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["args"] = args
                captured["kwargs"] = kwargs

        monkeypatch.setattr(ultrareview.subprocess, "Popen", _PopenSpy)
        ultrareview._default_spawn(["claude", "ultrareview", "1"])
        # Either `text=True` or `universal_newlines=True` — both mean
        # str-mode stdout. The implementation uses `text=True`.
        assert (
            captured["kwargs"].get("text") is True
            or captured["kwargs"].get("universal_newlines") is True
        )
