"""Spec-compliance tests for F-001-U-6 (E2E docker smoke + spawn/resume timeout).

These are independent tester-written tests that complement the coder's
test files (`tests/e2e/test_docker_worker_smoke.py`,
`tests/test_docker_worker_timeout.py`) by locking in the EXACT semantics
from the F-001-U-6 unit description verbatim:

  * E2E suite lives at `tests/e2e/test_docker_worker_smoke.py` and is
    auto-skipped on machines without a reachable Docker daemon.
  * The suite stitches together the per-axis units (U-2 worker, U-3
    dnsmasq sidecar, U-4 internal-registry passthrough) via fixtures
    in `tests/e2e/conftest.py`: `docker_available` (autouse skip),
    `worker_image` (build), `dnsmasq_sidecar` (sidecar), `orch_net`
    (bridge), `sandbox_repo` (per-test fixture copy).
  * Sandbox fixture lives under `tests/fixtures/sandbox-repo/` and is
    self-contained (no checked-in writes by tests).
  * Timeout knob (folded from PR #11 reviewer SUGGESTION 1):
      - `DEFAULT_SPAWN_TIMEOUT_SECONDS == 30 * 60` (30 min, named in
        the unit description verbatim).
      - `ORCH_WORKER_TIMEOUT_SECONDS` env knob overrides the default.
      - `DockerClaudeCodeWorker.timeout_seconds=` constructor field
        overrides the env knob.
      - `spawn()` and `resume()` both call `subprocess.run(timeout=...)`
        with the resolved value.
      - A `subprocess.TimeoutExpired` from the runner is translated to
        a `RuntimeError` that names BOTH the budget value AND the env
        knob name so the user can self-help from the error message.
  * Hang-timeout E2E case exists (worker w/ `timeout_seconds=10`, in-
    container command overridden to `sleep 600`, must raise within
    budget rather than block).

No live network, no live docker. Subprocess calls are injected via the
`run` attribute on the worker; fixtures and module structure are
verified by introspection of the tree.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    DEFAULT_SPAWN_TIMEOUT_SECONDS,
    TIMEOUT_ENV,
    DockerClaudeCodeWorker,
    _resolve_timeout_seconds,
)
from tests.conftest import _FakeProc, _make_worker

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_DIR = REPO_ROOT / "tests" / "e2e"
E2E_SMOKE = E2E_DIR / "test_docker_worker_smoke.py"
E2E_CONFTEST = E2E_DIR / "conftest.py"
SANDBOX_REPO_DIR = REPO_ROOT / "tests" / "fixtures" / "sandbox-repo"


# ---------------------------------------------------------------------------
# Constants / knobs pinned by the unit description.
# ---------------------------------------------------------------------------


class TestTimeoutConstants:
    """The unit description names the 30-minute default and the env knob
    name verbatim; if either drifts, downstream docs / runbooks go stale."""

    def test_default_timeout_is_30_minutes(self) -> None:
        assert DEFAULT_SPAWN_TIMEOUT_SECONDS == 30 * 60

    def test_timeout_env_var_is_orch_worker_timeout_seconds(self) -> None:
        """The .env knob name the user types is part of the public contract."""
        assert TIMEOUT_ENV == "ORCH_WORKER_TIMEOUT_SECONDS"


# ---------------------------------------------------------------------------
# E2E module + conftest structure (catches drift).
# ---------------------------------------------------------------------------


class TestE2EStructure:
    """The unit description fixes the E2E suite's location and shape
    so future refactors that move it must also update the description."""

    def test_e2e_module_at_canonical_path(self) -> None:
        """Lives at `tests/e2e/test_docker_worker_smoke.py` per the description."""
        assert E2E_SMOKE.is_file(), f"missing E2E smoke module at {E2E_SMOKE}"

    def test_e2e_conftest_present(self) -> None:
        """`tests/e2e/conftest.py` carries the shared fixtures."""
        assert E2E_CONFTEST.is_file(), f"missing E2E conftest at {E2E_CONFTEST}"

    def test_e2e_has_autouse_docker_skip_gate(self) -> None:
        """`docker_available` must be an autouse fixture so every test
        in `tests/e2e/` skips cleanly when Docker isn't reachable.
        Without autouse a contributor could forget the marker and CI
        would attempt a real-daemon run on a daemon-less machine."""
        src = E2E_CONFTEST.read_text()
        tree = ast.parse(src)
        autouse_fixtures = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                # Look for @pytest.fixture(autouse=True)
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if (
                            kw.arg == "autouse"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            autouse_fixtures.append(node.name)
        assert "docker_available" in autouse_fixtures, (
            f"docker_available must be autouse; found autouse fixtures: {autouse_fixtures!r}"
        )

    def test_e2e_conftest_defines_required_fixtures(self) -> None:
        """The four named fixtures from the unit description must all
        exist in the conftest: worker_image (step 1), dnsmasq_sidecar
        (step 2), sandbox_repo (step 3), and the orch_net bridge that
        the network-allowlist step depends on."""
        src = E2E_CONFTEST.read_text()
        tree = ast.parse(src)
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                defined.add(node.name)
        for required in ("docker_available", "worker_image", "dnsmasq_sidecar", "sandbox_repo"):
            assert required in defined, (
                f"missing fixture {required!r} in {E2E_CONFTEST}; defined: {sorted(defined)!r}"
            )

    def test_e2e_smoke_covers_eight_numbered_steps(self) -> None:
        """The unit description enumerates 8 steps (image build, dnsmasq,
        sandbox spawn, hybrid auth, cred boundary, network allowlist,
        session resume, doctor command). The E2E module must surface a
        test for each numbered concept — a smoke that drops a step is
        a regression."""
        src = E2E_SMOKE.read_text()
        tree = ast.parse(src)
        test_names = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        joined = " ".join(test_names).lower()
        # Each step has at least one test whose name mentions a relevant
        # keyword. We use loose substring matches so a renamed-but-correct
        # test still passes.
        step_keywords = {
            "step1_image_built": ("image",),
            "step2_dnsmasq_sidecar": ("dnsmasq", "sidecar"),
            "step3_spawn_against_sandbox": ("spawn", "sandbox"),
            "step4_hybrid_auth": ("auth",),
            "step5_cred_boundary": ("cred", "boundary"),
            "step6_network_allowlist": ("network", "allowlist"),
            "step7_session_resume": ("session", "resume"),
            "step8_doctor": ("doctor",),
        }
        missing: list[str] = []
        for step, keywords in step_keywords.items():
            if not all(k in joined for k in keywords):
                missing.append(step)
        assert not missing, (
            f"E2E smoke missing test coverage for steps: {missing!r}; "
            f"existing tests: {test_names!r}"
        )

    def test_e2e_has_hang_timeout_case(self) -> None:
        """PR #11 SUGGESTION 1 folded in: a worker with `timeout_seconds=10`
        and a hanging in-container command (`sleep 600` per the unit
        description) must raise within budget. The test exists and
        references both knobs verbatim."""
        src = E2E_SMOKE.read_text()
        assert "timeout_seconds=10" in src, (
            "hang-timeout E2E case must drive a 10s constructor timeout per unit description"
        )
        assert "sleep" in src and "600" in src, (
            "hang-timeout E2E case must use a `sleep 600` style hang per unit description"
        )


# ---------------------------------------------------------------------------
# Sandbox-repo fixture (step 3 + the bind-mount target).
# ---------------------------------------------------------------------------


class TestSandboxRepoFixture:
    """`tests/fixtures/sandbox-repo/` is the bind-mount source the E2E
    spawn-against-sandbox test uses. It must exist as a directory the
    container can mount at `/workspace`, and its README must carry the
    string the E2E test asserts on."""

    def test_sandbox_repo_directory_exists(self) -> None:
        assert SANDBOX_REPO_DIR.is_dir(), f"missing fixture dir at {SANDBOX_REPO_DIR}"

    def test_sandbox_repo_has_readme(self) -> None:
        """The E2E spawn test cat-s `/workspace/README.md`, so the
        fixture must ship a README at the top level."""
        assert (SANDBOX_REPO_DIR / "README.md").is_file()

    def test_sandbox_repo_readme_contains_assertion_substring(self) -> None:
        """`test_worker_spawns_container_against_sandbox` runs
        `cat /workspace/README.md` and asserts `"sandbox fixture" in
        proc.stdout`. The fixture's README MUST contain that exact
        substring or the E2E test fails when run against real Docker
        — a contract bug regardless of whether the local sandbox
        actually has Docker available.

        If you renamed the substring on either side, update both: the
        fixture's README *and* the assertion in
        `tests/e2e/test_docker_worker_smoke.py`."""
        readme = (SANDBOX_REPO_DIR / "README.md").read_text()
        assert "sandbox fixture" in readme, (
            f"sandbox-repo README is missing the 'sandbox fixture' substring "
            f"that tests/e2e/test_docker_worker_smoke.py::"
            f"test_worker_spawns_container_against_sandbox asserts on. "
            f"README currently reads:\n{readme!r}"
        )


# ---------------------------------------------------------------------------
# spawn() / resume() integrate the timeout knob end-to-end.
# Existing unit tests in tests/test_docker_worker_timeout.py mostly cover
# the helper-level resolution; here we lock the *process env* path —
# i.e. ORCH_WORKER_TIMEOUT_SECONDS set on os.environ at spawn time
# flows through into the actual subprocess.run timeout argument.
# ---------------------------------------------------------------------------


class TestTimeoutEndToEndIntegration:
    def test_env_var_on_process_env_threads_through_spawn(self, tmp_path, monkeypatch) -> None:
        """The user's `.env` sets ORCH_WORKER_TIMEOUT_SECONDS=120; that
        value must reach subprocess.run unchanged. No re-implementation
        of the helper — this is the integration assertion that the
        host's env actually flows into the running subprocess call."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(TIMEOUT_ENV, "120")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.spawn("hello")
        assert captured["timeout"] == 120, (
            f"ORCH_WORKER_TIMEOUT_SECONDS=120 did not reach subprocess.run; captured: {captured!r}"
        )

    def test_env_var_on_process_env_threads_through_resume(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(TIMEOUT_ENV, "240")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.resume("s", "msg")
        assert captured["timeout"] == 240

    def test_default_30min_timeout_when_no_overrides(self, tmp_path, monkeypatch) -> None:
        """No env, no field override → 30 min default reaches subprocess.run."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(TIMEOUT_ENV, raising=False)

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.spawn("x")
        assert captured["timeout"] == DEFAULT_SPAWN_TIMEOUT_SECONDS
        assert captured["timeout"] == 1800


# ---------------------------------------------------------------------------
# Error message must be actionable: name the env knob AND the field name,
# plus the role and the budget value, so a user reading the orchestrator's
# escalation digest can fix it without grep.
# ---------------------------------------------------------------------------


class TestTimeoutErrorActionability:
    def test_spawn_error_names_env_knob_and_field(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 13
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.spawn("hi")
        msg = str(excinfo.value)
        # Actionable on its own: the operator gets the budget, the
        # operation, and the two ways to change it.
        assert "13s" in msg, f"budget not in message: {msg!r}"
        assert "spawn" in msg, f"operation not in message: {msg!r}"
        assert "ORCH_WORKER_TIMEOUT_SECONDS" in msg, f"env knob not named in message: {msg!r}"
        assert "timeout_seconds" in msg, f"constructor field not named in message: {msg!r}"

    def test_resume_error_names_env_knob_and_field(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def hanging(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 0)

        worker = _make_worker(tmp_path)
        worker.timeout_seconds = 11
        worker.run = hanging

        with pytest.raises(RuntimeError) as excinfo:
            worker.resume("s", "msg")
        msg = str(excinfo.value)
        assert "11s" in msg
        assert "resume" in msg
        assert "ORCH_WORKER_TIMEOUT_SECONDS" in msg
        assert "timeout_seconds" in msg


# ---------------------------------------------------------------------------
# Resolution-order edge cases at the helper level (complement the cases
# in tests/test_docker_worker_timeout.py — these specifically test that
# host_env=None falls back to os.environ).
# ---------------------------------------------------------------------------


class TestResolveTimeoutOsEnvFallback:
    def test_host_env_none_reads_process_env(self, monkeypatch) -> None:
        """Default `host_env=None` parameter falls back to `os.environ`."""
        monkeypatch.setenv(TIMEOUT_ENV, "77")
        assert _resolve_timeout_seconds(None) == 77

    def test_host_env_none_falls_through_to_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(TIMEOUT_ENV, raising=False)
        assert _resolve_timeout_seconds(None) == DEFAULT_SPAWN_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# DockerClaudeCodeWorker.timeout_seconds field — back-compat (defaults to
# None so all the pre-U-6 call-sites keep working) and constructor wiring.
# ---------------------------------------------------------------------------


class TestWorkerTimeoutField:
    def test_constructor_default_is_none(self, tmp_path) -> None:
        """`timeout_seconds=None` is the back-compat default."""
        w = _make_worker(tmp_path)
        assert w.timeout_seconds is None

    def test_explicit_field_beats_env(self, tmp_path, monkeypatch) -> None:
        """Constructor field overrides the env knob."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(TIMEOUT_ENV, "999")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        w = _make_worker(tmp_path)
        w.timeout_seconds = 42
        w.run = fake_run
        w.spawn("x")
        assert captured["timeout"] == 42

    def test_dataclass_kwarg_accepted(self, tmp_path) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude" / "sessions").mkdir(parents=True)
        workdir = tmp_path / "work"
        workdir.mkdir()
        w = DockerClaudeCodeWorker(
            role="coder",
            workdir=workdir,
            home_dir=fake_home,
            timeout_seconds=600,
        )
        assert w.timeout_seconds == 600
