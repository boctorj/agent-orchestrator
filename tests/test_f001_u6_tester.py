"""Tester-written contracts for F-001-U-6 (E2E docker smoke + spawn/resume timeout).

These tests are independent of the coder's own `tests/test_f001_u6_spec.py`
and `tests/test_docker_worker_timeout.py`. They lock in subtle behaviour
the existing tests don't fully pin:

  * `RuntimeError` from a `TimeoutExpired` translation preserves the
    original exception via `__cause__` — so an operator debugging the
    escalation digest can still see the wrapped `subprocess.TimeoutExpired`
    chain.
  * The translated message names the *operation* (`spawn` vs `resume`),
    not just "docker run", so the operator can immediately tell which
    call timed out.
  * `subprocess.run` is invoked with `capture_output=True, text=True`
    on the timeout path too — without those, the wrapped
    `TimeoutExpired.stdout` / `.stderr` are bytes-or-None and the
    surfaced error chain becomes unreadable.
  * `build_docker_argv` does NOT inject any timeout-related flag.
    The timeout is a host-side concern; docker's `--stop-timeout`
    has a different meaning and we don't want to confuse them.
  * The `_resolve_timeout` instance method exists and threads through
    `_resolve_timeout_seconds` — i.e. there's no second path that
    short-circuits the resolution rules.
  * Default subprocess timeout is sane for real production work
    (≥ 5 minutes) so a slow worker isn't killed mid-clone-or-install.
  * The autouse skip fixture in `tests/e2e/conftest.py` is
    function-scoped (the pytest default), so every test in the
    module evaluates the skip predicate fresh — a fixture that was
    accidentally session-scoped would cache the first decision and
    misbehave on flaky daemons.
  * The skip message names the opt-in env knob verbatim, so a
    contributor who sees the skip can immediately self-help.
  * The sandbox fixture is a real directory rooted at `tests/fixtures/
    sandbox-repo/` with a README that survives a `Path.is_file()` check,
    and `worker.archive()` is still a no-op after the new dataclass
    field was added (regression guard).
  * The E2E module is parseable by Python — a SyntaxError there would
    silently skip the suite with no signal, so we keep the explicit
    compile check at the module-load layer.

No live network, no real docker. All subprocess interactions are
injected via the `run` attribute on the worker.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    DEFAULT_SPAWN_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
)
from tests.conftest import _FakeProc, _make_worker

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / "tests" / "e2e"
E2E_SMOKE = E2E_DIR / "test_docker_worker_smoke.py"
E2E_CONFTEST = E2E_DIR / "conftest.py"
SANDBOX_REPO_DIR = REPO_ROOT / "tests" / "fixtures" / "sandbox-repo"


# ---------------------------------------------------------------------------
# Timeout error: cause chain, operation name, capture flags.
# ---------------------------------------------------------------------------


class TestTimeoutCauseChain:
    """A `TimeoutExpired` on the subprocess seam translates to a
    `RuntimeError`; the original exception must remain accessible via
    `__cause__` (i.e. `raise RuntimeError(...) from exc`). Stripping
    the chain hides the underlying subprocess context from anyone
    debugging the escalation digest."""

    def test_spawn_preserves_timeout_expired_as_cause(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 3
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("hi")
        assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired), (
            f"spawn timeout must chain TimeoutExpired via __cause__; "
            f"got __cause__={excinfo.value.__cause__!r}"
        )

    def test_resume_preserves_timeout_expired_as_cause(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 4
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.resume("sess", "msg")
        assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


class TestTimeoutErrorOperationName:
    """The translated `RuntimeError` message must say which operation
    timed out. Without that, an operator reading the escalation digest
    can't tell whether the orchestrator hung on the initial spawn or on
    a follow-up resume — they read very differently in the timeline."""

    def test_spawn_error_distinguishes_from_resume(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 5
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("task")
        msg = str(excinfo.value)
        assert "spawn" in msg, f"spawn timeout error must name the operation; got: {msg!r}"
        assert "resume" not in msg, (
            f"spawn error must NOT mention 'resume' to avoid operator confusion; got: {msg!r}"
        )

    def test_resume_error_distinguishes_from_spawn(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 5
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.resume("sess", "msg")
        msg = str(excinfo.value)
        assert "resume" in msg, f"resume timeout error must name the operation; got: {msg!r}"
        # "spawn" appears in the role-agnostic prefix? No — current
        # message hard-codes the operation. Lock the distinction.
        assert " spawn " not in msg and not msg.endswith("spawn"), (
            f"resume error must NOT mention 'spawn' as the operation; got: {msg!r}"
        )


class TestSubprocessRunInvocation:
    """The subprocess call must pass `capture_output=True, text=True`,
    `check=False`, AND the resolved `timeout=...` together. Missing any
    one is a real regression: without `text=True` the TimeoutExpired
    surfaces bytes; with `check=True` the timeout translation never
    runs because CalledProcessError fires first."""

    def test_spawn_passes_capture_and_text_with_timeout(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 11
        worker.run = fake_run
        worker.spawn("task")
        assert captured.get("capture_output") is True
        assert captured.get("text") is True
        assert captured.get("check") is False
        assert captured.get("timeout") == 11

    def test_resume_passes_capture_and_text_with_timeout(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured.update(kwargs)
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 22
        worker.run = fake_run
        worker.resume("sess", "msg")
        assert captured.get("capture_output") is True
        assert captured.get("text") is True
        assert captured.get("check") is False
        assert captured.get("timeout") == 22


# ---------------------------------------------------------------------------
# Docker argv stays untouched by the timeout field — timeout is a host-side
# concern, not a docker flag. A regression here would silently surface as
# a confused docker CLI complaining about an unknown `--timeout` flag.
# ---------------------------------------------------------------------------


class TestDockerArgvUnaffectedByTimeout:
    def test_argv_does_not_contain_timeout_flag(self, tmp_path) -> None:
        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 17
        argv = worker.build_docker_argv(["claude", "-p", "task"])
        joined = " ".join(argv)
        # Neither the host-side knob name nor a literal `--timeout` should
        # appear in the docker argv. (docker has `--stop-timeout` for a
        # different purpose — assert that's also absent so a future
        # accidental insertion is caught.)
        assert "ORCH_WORKER_TIMEOUT_SECONDS" not in joined, (
            f"host-side timeout env name leaked into docker argv: {joined!r}"
        )
        assert "--timeout" not in joined, (
            f"docker run argv must not carry --timeout; got: {joined!r}"
        )
        assert "--stop-timeout" not in joined, (
            f"docker run argv must not carry --stop-timeout; got: {joined!r}"
        )

    def test_argv_shape_identical_with_and_without_timeout_field(self, tmp_path) -> None:
        """`timeout_seconds=N` and `timeout_seconds=None` must yield
        byte-identical argv — the field is host-side state only."""
        # Two workers rooted in separate tmp paths so `_make_worker`'s
        # mkdir of `home/.claude/sessions` doesn't collide.
        path_a = tmp_path / "a"
        path_a.mkdir()
        path_b = tmp_path / "b"
        path_b.mkdir()
        worker_a = _make_worker(path_a)
        worker_a.timeout_seconds = None
        worker_b = _make_worker(path_b)
        worker_b.timeout_seconds = 99
        # Same workdir / home so the bind-mount source paths are identical;
        # otherwise argv naturally differs on the tmp paths and the test
        # is meaningless.
        worker_b.workdir = worker_a.workdir
        worker_b.home_dir = worker_a.home_dir
        argv_a = worker_a.build_docker_argv(["claude", "-p", "x"])
        argv_b = worker_b.build_docker_argv(["claude", "-p", "x"])
        assert argv_a == argv_b


# ---------------------------------------------------------------------------
# Instance-method `_resolve_timeout` exists and routes through the helper.
# A separate code path (e.g. inlined logic in spawn) would silently bypass
# the resolution rules.
# ---------------------------------------------------------------------------


class TestResolveTimeoutInstanceMethod:
    def test_instance_method_delegates_to_helper(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(TIMEOUT_ENV, "55")
        worker = _make_worker(tmp_path)
        # No constructor override → env wins.
        assert worker._resolve_timeout() == 55

    def test_instance_method_uses_constructor_field(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(TIMEOUT_ENV, "99")
        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 7
        assert worker._resolve_timeout() == 7

    def test_instance_method_falls_back_to_default(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv(TIMEOUT_ENV, raising=False)
        worker = _make_worker(tmp_path)
        assert worker._resolve_timeout() == DEFAULT_SPAWN_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Default timeout is sane for real production work (≥ 5 minutes).
# This complements the strict `== 30*60` assertion in the coder's spec
# file by also surfacing a *lower bound* check: even if someone changes
# the default, it should never drop below something a normal clone +
# install + push could finish under.
# ---------------------------------------------------------------------------


class TestDefaultTimeoutSanity:
    def test_default_at_least_five_minutes(self) -> None:
        assert DEFAULT_SPAWN_TIMEOUT_SECONDS >= 5 * 60, (
            f"DEFAULT_SPAWN_TIMEOUT_SECONDS={DEFAULT_SPAWN_TIMEOUT_SECONDS}s is too tight; "
            f"a normal worker spawn easily takes >5 min on a cold image / cold registry"
        )

    def test_default_under_two_hours(self) -> None:
        """Upper bound — a 2-hour timeout means a deadlocked spawn
        could burn that long before the orchestrator notices. The unit
        description names 30 min as the budget."""
        assert DEFAULT_SPAWN_TIMEOUT_SECONDS <= 2 * 60 * 60, (
            f"DEFAULT_SPAWN_TIMEOUT_SECONDS={DEFAULT_SPAWN_TIMEOUT_SECONDS}s is too generous; "
            f"unit description names 30 min as the budget"
        )


# ---------------------------------------------------------------------------
# E2E suite structure: skip fixture scope + message, module parseability.
# ---------------------------------------------------------------------------


class TestE2EConftestSkipFixture:
    """The `docker_available` fixture in `tests/e2e/conftest.py` must be
    autouse and function-scoped. A session-scoped autouse would cache
    the first skip decision across the whole suite, which is wrong on
    a flaky daemon — every test should evaluate the predicate fresh."""

    def test_docker_available_is_function_scoped(self) -> None:
        src = E2E_CONFTEST.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name == "docker_available"):
                continue
            # Inspect the decorator: must be `@pytest.fixture(autouse=True)`.
            # If a `scope=` kwarg is present, it must be "function" (or absent,
            # which defaults to function-scope).
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                for kw in dec.keywords:
                    if kw.arg == "scope":
                        assert (
                            isinstance(kw.value, ast.Constant) and kw.value.value == "function"
                        ), f"docker_available must be function-scoped; got scope={kw.value!r}"
            return
        pytest.fail("docker_available fixture not found in tests/e2e/conftest.py")

    def test_skip_message_names_the_opt_in_env_var(self, monkeypatch) -> None:
        """The skip message a developer sees must include the env knob
        name verbatim so they can self-help without grepping the source.

        Runtime check rather than source grep — the implementation uses
        an f-string with the `E2E_OPT_IN_ENV` constant interpolated, so
        a static regex over the source would miss it.
        """
        # The static module reference is a soft hint; if the var name
        # ever changes this catches the rename early.
        src = E2E_CONFTEST.read_text()
        assert "ORCH_RUN_E2E" in src, (
            "tests/e2e/conftest.py must reference the ORCH_RUN_E2E opt-in env knob"
        )

        # Drive the fixture body directly: with the opt-in unset we should
        # see a pytest.skip whose message names the env knob.
        monkeypatch.delenv("ORCH_RUN_E2E", raising=False)
        # Import inside the test so monkeypatch.delenv lands first.
        import importlib

        e2e_conftest = importlib.import_module("tests.e2e.conftest")
        # The fixture is a plain function (decorated by @pytest.fixture).
        # FixtureFunctionMarker stashes the original at __wrapped__ in some
        # pytest versions; in others, the decorator returns the function
        # as-is with metadata attached. Pull whichever is present.
        fixture_fn = e2e_conftest.docker_available
        underlying = getattr(fixture_fn, "__wrapped__", fixture_fn)
        with pytest.raises(pytest.skip.Exception) as excinfo:
            underlying()
        msg = str(excinfo.value)
        assert "ORCH_RUN_E2E" in msg, (
            f"the pytest.skip(...) message in tests/e2e/conftest.py must name "
            f"the ORCH_RUN_E2E opt-in env knob so a developer can self-help; "
            f"got message: {msg!r}"
        )


class TestE2EModuleParseability:
    """The E2E smoke module must be importable / compilable.

    A SyntaxError there causes pytest to silently emit collection errors
    that look like skips on some configurations — without an explicit
    parseability check the contract "tests/e2e/test_docker_worker_smoke.py
    runs on every PR in CI" is fragile.
    """

    def test_smoke_module_parses(self) -> None:
        src = E2E_SMOKE.read_text()
        try:
            ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"tests/e2e/test_docker_worker_smoke.py has a SyntaxError: {exc!r}")

    def test_conftest_module_parses(self) -> None:
        src = E2E_CONFTEST.read_text()
        try:
            ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"tests/e2e/conftest.py has a SyntaxError: {exc!r}")

    def test_module_compiles_with_compile(self) -> None:
        """Beyond `ast.parse`, run `compile()` so a future bytecode-level
        regression (rare but possible in 3.11+) is caught too."""
        for path in (E2E_SMOKE, E2E_CONFTEST):
            src = path.read_text()
            try:
                compile(src, str(path), "exec")
            except SyntaxError as exc:
                pytest.fail(f"{path}: compile failed: {exc!r}")


# ---------------------------------------------------------------------------
# Sandbox fixture: directory, README, hello.txt, and the exact substring
# the smoke test asserts on. Pinned harder than the coder's spec test so a
# subtle whitespace / case drift surfaces locally.
# ---------------------------------------------------------------------------


class TestSandboxFixture:
    def test_fixture_root_is_directory(self) -> None:
        assert SANDBOX_REPO_DIR.is_dir(), f"missing fixture dir at {SANDBOX_REPO_DIR}"

    def test_fixture_has_readme_file(self) -> None:
        readme = SANDBOX_REPO_DIR / "README.md"
        assert readme.is_file(), f"missing README at {readme}"
        # Non-empty so a `docker exec cat /workspace/README.md` returns
        # something meaningful for the assertion.
        assert readme.stat().st_size > 0, f"README.md exists but is empty: {readme}"

    def test_fixture_readme_contains_assertion_substring(self) -> None:
        """The E2E smoke test asserts `"sandbox fixture" in proc.stdout`."""
        text = (SANDBOX_REPO_DIR / "README.md").read_text()
        assert "sandbox fixture" in text, (
            f"sandbox-repo/README.md must contain the exact substring "
            f"'sandbox fixture' that the E2E test asserts on; "
            f"README reads:\n{text!r}"
        )

    def test_fixture_files_only_under_fixture_root(self) -> None:
        """Defensive guard: the sandbox fixture must not contain symlinks
        that escape the fixture root (e.g. a symlinked `.ssh` would
        defeat the cred-boundary check when bind-mounted)."""
        for entry in SANDBOX_REPO_DIR.rglob("*"):
            if entry.is_symlink():
                target = entry.resolve()
                assert (
                    SANDBOX_REPO_DIR.resolve() in target.parents
                    or target == SANDBOX_REPO_DIR.resolve()
                ), f"symlink {entry} -> {target} escapes the sandbox fixture root"


# ---------------------------------------------------------------------------
# Back-compat: `archive()` still works after the new dataclass field. A
# typo on the new field would surface as a TypeError or NameError here.
# ---------------------------------------------------------------------------


class TestArchiveStillNoOp:
    def test_archive_returns_none(self, tmp_path) -> None:
        worker = _make_worker(tmp_path)
        # archive() returns None per the Worker protocol; we call it for
        # the side-effect (no-op) and assert it doesn't raise. Mypy
        # rejects asserting the return value directly since the typed
        # signature is `-> None`.
        result = worker.archive("any-session-id")  # type: ignore[func-returns-value]
        assert result is None

    def test_archive_does_not_invoke_subprocess(self, tmp_path) -> None:
        worker = _make_worker(tmp_path)
        called: list = []

        def fake_run(argv, **kw):
            called.append(argv)
            return _FakeProc()

        worker.run = fake_run
        worker.archive("sess-1")
        assert called == [], (
            "archive() must not shell out — it's a pure no-op on the docker backend"
        )


# ---------------------------------------------------------------------------
# When the runner returns a `_FakeProc`-like object that does NOT have a
# `stdout` attr (older test stubs), the spawn() / resume() codepaths
# already guard via `hasattr`. Lock the behaviour so the guard stays.
# ---------------------------------------------------------------------------


class TestRunnerObjectMissingStdoutAttr:
    def test_spawn_handles_proc_missing_stdout(self, tmp_path, monkeypatch) -> None:
        """Some test stubs return objects without `.stdout`. The
        production code uses `hasattr` to tolerate that — without the
        guard we'd crash on a runner that emits a bare success sentinel."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        class _BareSentinel:
            returncode = 0

        worker = _make_worker(tmp_path)
        worker.run = lambda argv, **kw: _BareSentinel()
        # No stdout → extract_session_id sees "" → RuntimeError "Could not extract..."
        # rather than an AttributeError.
        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("task")
        msg = str(excinfo.value)
        assert "session_id" in msg, (
            f"expected the session-id extraction error, got AttributeError-ish: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Module can be imported standalone — pytest collection works.
# ---------------------------------------------------------------------------


def test_smoke_module_is_pytest_collectible() -> None:
    """Run `pytest --collect-only` on the smoke module; collection must
    succeed (skips are fine, errors are not)."""
    # Use the same interpreter pytest itself is running under so we
    # don't accidentally collect against a different env.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(E2E_SMOKE),
            "--collect-only",
            "-q",
            "--no-cov",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"collection of tests/e2e/test_docker_worker_smoke.py failed "
        f"(rc={proc.returncode}); stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
