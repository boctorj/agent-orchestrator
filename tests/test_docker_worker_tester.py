"""Tester-authored complementary suite for F-001-U-2 (Docker worker MVP).

The coder shipped per-axis files (`test_docker_worker_auth.py`,
`...flags.py`, `...cred_boundary.py`, `...session.py`,
`test_doctor_cred_audit.py`). This file is an independent second-pass
verification of behaviors named in the unit description that the
per-axis files don't directly pin:

  1. **Dockerfile content** — the image is part of the deliverable.
     Unit description: "Python 3.12 + Node + git + gh + claude CLI;
     runs as --user 1000:1000". Verify the Dockerfile asks for these.
  2. **Mount directions are exact** — "${workdir}:/workspace (rw)" and
     "~/.claude/sessions (rw)" are explicit; only `~/.claude` in OAuth
     mode carries `readonly`. Pin the bind directions byte-for-byte.
  3. **Security-critical `--env NAME` form** — argv must use the
     value-less passthrough form (so secrets stay out of `ps`/argv),
     with the actual value handed only via subprocess env.
  4. **archive() is a no-op** — Worker-protocol completeness.
  5. **spawn() returns (session_id, response_text)** — the contract
     callers (state.db persistence) depend on.
  6. **make_worker("...") under ORCH_WORKER_BACKEND=docker** returns a
     real DockerClaudeCodeWorker, not a placeholder.

All tests mock subprocess; no real Docker daemon is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    AGENT_GID,
    AGENT_UID,
    DEFAULT_IMAGE,
    DEFAULT_NETWORK,
    DockerClaudeCodeWorker,
    build_cred_audit,
    select_auth_mode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(tmp_path: Path) -> DockerClaudeCodeWorker:
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "sessions").mkdir(parents=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    return DockerClaudeCodeWorker(role="coder", workdir=workdir, home_dir=fake_home)


class _FakeProc:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _mount_values(argv: list[str]) -> list[str]:
    """Return every value that follows a `--mount` flag (in order)."""
    out: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--mount" and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


# ---------------------------------------------------------------------------
# 1. Dockerfile content — the image is a deliverable named by the unit.
# ---------------------------------------------------------------------------


class TestDockerfileContent:
    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        # docker/worker.Dockerfile is at the repo root.
        path = Path(__file__).parent.parent / "docker" / "worker.Dockerfile"
        assert path.exists(), f"Dockerfile missing at {path}"
        return path.read_text()

    def test_dockerfile_uses_python_3_12_base(self, dockerfile):
        """Unit description: "Python 3.12"."""
        assert "python:3.12" in dockerfile, (
            "Dockerfile must use a python:3.12 base image; unit says Python 3.12"
        )

    def test_dockerfile_installs_node(self, dockerfile):
        """Unit description: "Node". Claude Code is an npm package, so node
        is required."""
        text = dockerfile.lower()
        assert "node" in text, "Dockerfile must install Node (claude is an npm package)"

    def test_dockerfile_installs_git_and_gh(self, dockerfile):
        """Unit description: "git + gh"."""
        # git is in the apt-get install line; gh is too.
        assert "git" in dockerfile
        assert " gh " in dockerfile or "\tgh " in dockerfile or "gh\n" in dockerfile, (
            "Dockerfile must install the `gh` CLI"
        )

    def test_dockerfile_installs_claude_cli(self, dockerfile):
        """Unit description: "claude CLI". The npm package name is
        `@anthropic-ai/claude-code`."""
        assert "@anthropic-ai/claude-code" in dockerfile, (
            "Dockerfile must npm-install @anthropic-ai/claude-code (the claude CLI)"
        )

    def test_dockerfile_creates_agent_user_uid_1000(self, dockerfile):
        """Unit description: "runs as --user 1000:1000". The Dockerfile
        must create an agent user with uid 1000 so the runtime --user
        flag matches an existing user (otherwise file ownership breaks)."""
        assert "1000" in dockerfile, "Dockerfile must reference uid 1000"
        # And it must switch to that user (USER agent) so the default
        # container process isn't root.
        assert "\nUSER agent" in dockerfile or "\nUSER 1000" in dockerfile, (
            "Dockerfile must drop privileges with `USER agent` / `USER 1000`"
        )


# ---------------------------------------------------------------------------
# 2. Mount directions are exact.
# ---------------------------------------------------------------------------


class TestMountDirections:
    """Pin the exact bind-mount directions named in the unit description.

    ${workdir}:/workspace (rw)
    ~/.claude:/home/agent/.claude:ro (OAuth only)
    ~/.claude/sessions (rw)
    """

    def test_workspace_mount_is_rw_by_omission_of_readonly(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        ws_mounts = [m for m in _mount_values(argv) if "target=/workspace" in m]
        assert len(ws_mounts) == 1, f"expected exactly one /workspace mount; got {ws_mounts!r}"
        # The `readonly` flag is the only way to make a docker bind mount RO;
        # its absence == read-write. The description requires rw.
        assert "readonly" not in ws_mounts[0], (
            f"/workspace mount must be rw (description says (rw)); got: {ws_mounts[0]!r}"
        )

    def test_sessions_mount_is_rw_in_both_auth_modes(self, tmp_path):
        """Description: "~/.claude/sessions (rw, session resume)". The
        OAuth lifecycle requires write-back to this directory for refresh
        token persistence."""
        worker = _make_worker(tmp_path)
        for host_env in (
            {"GITHUB_TOKEN": "ghp_x"},  # oauth
            {"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-x"},  # api-key
        ):
            argv = worker.build_docker_argv(["claude", "-p", "x"], host_env=host_env)
            sess = [m for m in _mount_values(argv) if "target=/home/agent/.claude/sessions" in m]
            assert len(sess) == 1, f"expected one sessions mount for env={host_env!r}; got {sess!r}"
            assert "readonly" not in sess[0], (
                "sessions mount must be writable in BOTH modes "
                f"(env={host_env!r}); got: {sess[0]!r}"
            )

    def test_claude_dir_mount_carries_readonly_in_oauth_mode(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        claude_mounts = [
            m
            for m in _mount_values(argv)
            # `target=/home/agent/.claude` but NOT `.../sessions`.
            if m.rstrip(",readonly").endswith("target=/home/agent/.claude")
            or ",target=/home/agent/.claude," in m
        ]
        assert len(claude_mounts) == 1, (
            f"OAuth mode must mount exactly one ~/.claude target; got {claude_mounts!r}"
        )
        assert "readonly" in claude_mounts[0], (
            f"OAuth ~/.claude mount must be ro (description: 'ro'); got: {claude_mounts[0]!r}"
        )


# ---------------------------------------------------------------------------
# 3. Security-critical: --env passthrough is the value-less form.
# ---------------------------------------------------------------------------


class TestEnvPassthroughIsValueLess:
    """Description: "Strict credential boundary". The hardened path is
    `--env NAME` (docker pulls NAME from its own subprocess env) — NOT
    `--env NAME=secret`, which would expose the secret in `ps` and in
    any shell-trace logging.
    """

    def test_argv_uses_valueless_env_for_github_token(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={"GITHUB_TOKEN": "ghp_supersecret_value"},
        )
        # No occurrence of GITHUB_TOKEN=<anything> in argv tokens.
        for tok in argv:
            assert not tok.startswith("GITHUB_TOKEN="), (
                f"argv must not embed GITHUB_TOKEN value; got token: {tok!r}"
            )
        # And the secret value itself must not leak into argv at all.
        joined = " ".join(argv)
        assert "ghp_supersecret_value" not in joined, "GITHUB_TOKEN value leaked into rendered argv"
        # The whitelist token still appears via the `--env NAME` form.
        assert "GITHUB_TOKEN" in argv

    def test_argv_uses_valueless_env_for_anthropic_api_key(self, tmp_path):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={
                "ANTHROPIC_API_KEY": "sk-ant-supersecret",
                "GITHUB_TOKEN": "ghp_x",
            },
        )
        for tok in argv:
            assert not tok.startswith("ANTHROPIC_API_KEY="), (
                f"argv must not embed ANTHROPIC_API_KEY value; got: {tok!r}"
            )
        assert "sk-ant-supersecret" not in " ".join(argv), (
            "ANTHROPIC_API_KEY value leaked into rendered argv"
        )

    def test_subprocess_env_carries_secret_value_when_whitelisted(self, tmp_path):
        """The mirror invariant: the curated subprocess env handed to
        docker DOES carry the secret value (otherwise docker can't
        forward it). The boundary is positive whitelist + value-less argv
        — both halves are required."""
        worker = _make_worker(tmp_path)
        env = worker.build_subprocess_env(
            host_env={
                "ANTHROPIC_API_KEY": "sk-ant-supersecret",
                "GITHUB_TOKEN": "ghp_pat_value",
                "AWS_ACCESS_KEY_ID": "AKIA-leak",
            }
        )
        # Whitelisted values flow through with their actual values.
        assert env.get("GITHUB_TOKEN") == "ghp_pat_value"
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-supersecret"
        # Hostile var dropped entirely (neither name nor value).
        assert "AWS_ACCESS_KEY_ID" not in env


# ---------------------------------------------------------------------------
# 4. archive() is a no-op.
# ---------------------------------------------------------------------------


class TestArchiveNoOp:
    def test_archive_returns_none_and_makes_no_subprocess_call(self, tmp_path):
        worker = _make_worker(tmp_path)
        calls: list = []
        worker.run = lambda *a, **kw: calls.append((a, kw))  # type: ignore[assignment]
        result = worker.archive("sess-xyz")
        assert result is None
        assert calls == [], f"archive() must be a no-op; saw subprocess calls: {calls!r}"


# ---------------------------------------------------------------------------
# 5. spawn() return shape: (session_id, response_text).
# ---------------------------------------------------------------------------


class TestSpawnReturnShape:
    def test_spawn_returns_session_id_and_response_text(self, tmp_path, monkeypatch):
        """The caller (state.db persistence) destructures the tuple.
        Pin the contract."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        worker = _make_worker(tmp_path)
        worker.run = lambda *a, **kw: _FakeProc(
            stdout=json.dumps({"session_id": "sess-9", "result": "the assistant's reply text"}),
            returncode=0,
        )
        sid, resp = worker.spawn("do the thing")
        assert sid == "sess-9"
        assert resp == "the assistant's reply text"

    def test_spawn_invokes_subprocess_with_curated_env_not_os_environ(self, tmp_path, monkeypatch):
        """The strictness of the credential boundary depends on the
        subprocess being invoked with the CURATED env dict, not
        os.environ. Verify by setting a hostile var on the real process
        env and asserting it isn't present in the env handed to the
        subprocess runner."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
        monkeypatch.setenv("SSH_AUTH_SOCK", "should-not-leak-either")

        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker = _make_worker(tmp_path)
        worker.run = fake_run
        worker.spawn("hi")

        env = captured["env"]
        assert env is not None, "spawn must hand env=<dict> to subprocess.run"
        assert "AWS_SECRET_ACCESS_KEY" not in env, (
            f"hostile var leaked through to subprocess env: {sorted(env)!r}"
        )
        assert "SSH_AUTH_SOCK" not in env
        # The whitelisted token DID make it through.
        assert env.get("GITHUB_TOKEN") == "ghp_x"


# ---------------------------------------------------------------------------
# 6. Factory wiring: ORCH_WORKER_BACKEND=docker actually returns the
# docker worker (with role propagated). This duplicates one assertion in
# test_worker_factory.py on purpose — it's the integration point most
# likely to silently regress.
# ---------------------------------------------------------------------------


class TestFactoryWiring:
    def test_make_worker_docker_returns_docker_worker_with_role(self, monkeypatch):
        monkeypatch.setenv("ORCH_WORKER_BACKEND", "docker")
        from orchestrator.workers import make_worker

        w = make_worker("reviewer")
        assert isinstance(w, DockerClaudeCodeWorker)
        assert w.role == "reviewer"


# ---------------------------------------------------------------------------
# 7. Auth-mode log line happens BEFORE the subprocess call (so users see
# the chosen mode even if docker run hangs).
# ---------------------------------------------------------------------------


class TestAuthLogOrdering:
    def test_log_emitted_before_subprocess_runs(self, tmp_path, monkeypatch):
        """The unit description: "Log chosen mode at spawn". The point
        of logging at spawn is to see the mode immediately; if the log
        happened after subprocess.run returned, a hanging docker call
        would swallow the receipt."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        events: list[str] = []
        worker = _make_worker(tmp_path)
        worker.log = lambda msg: events.append(f"log:{msg}")

        def fake_run(argv, **kw):
            events.append("subprocess.run")
            return _FakeProc(stdout=json.dumps({"session_id": "s", "result": "ok"}), returncode=0)

        worker.run = fake_run
        worker.spawn("hi")

        assert "log:Auth: claude.ai OAuth" in events
        assert "subprocess.run" in events
        # The log call must precede the subprocess invocation.
        assert events.index("log:Auth: claude.ai OAuth") < events.index("subprocess.run"), (
            f"auth-mode log must precede subprocess call; events={events!r}"
        )


# ---------------------------------------------------------------------------
# 8. Defaults match the unit description's exact tokens.
# Independent of the parameterised flag test in `test_docker_worker_flags.py`
# — pins the module-level constants directly so a future "tune to 8g" tweak
# without updating the description is caught here.
# ---------------------------------------------------------------------------


class TestModuleDefaultsMatchSpec:
    def test_default_memory_is_4g(self):
        from orchestrator.workers.docker_claude_code import DEFAULT_MEMORY

        assert DEFAULT_MEMORY == "4g"

    def test_default_cpus_is_2(self):
        from orchestrator.workers.docker_claude_code import DEFAULT_CPUS

        assert DEFAULT_CPUS == "2"

    def test_default_pids_limit_is_512(self):
        from orchestrator.workers.docker_claude_code import DEFAULT_PIDS_LIMIT

        assert DEFAULT_PIDS_LIMIT == "512"

    def test_default_network_is_orch_net(self):
        assert DEFAULT_NETWORK == "orch-net"

    def test_default_image_tag_is_orchestrator_worker_latest(self):
        assert DEFAULT_IMAGE == "orchestrator/worker:latest"

    def test_agent_uid_gid_are_1000(self):
        assert AGENT_UID == 1000
        assert AGENT_GID == 1000


# ---------------------------------------------------------------------------
# 9. select_auth_mode is a pure function — no implicit os.environ read
# when host_env is passed (otherwise tests would leak between modes).
# ---------------------------------------------------------------------------


class TestSelectAuthModePure:
    def test_passes_empty_dict_yields_oauth(self):
        assert select_auth_mode({}) == "oauth"

    def test_explicit_host_env_overrides_real_environ(self, monkeypatch):
        """Set a real ANTHROPIC_API_KEY on the process; pass an empty
        host_env. Must still choose OAuth — the function must use the
        argument, not os.environ."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-on-the-host")
        assert select_auth_mode({}) == "oauth", (
            "select_auth_mode(host_env={}) must NOT fall back to os.environ"
        )


# ---------------------------------------------------------------------------
# 10. build_cred_audit consistency — the receipts the doctor command
# prints describe what the worker actually does. Verify they agree on
# the boundary (no need to re-snapshot the full text; just cross-check).
# ---------------------------------------------------------------------------


class TestAuditAgreesWithArgv:
    @pytest.mark.parametrize(
        ("host_env", "expected_mode"),
        [
            ({"GITHUB_TOKEN": "g"}, "oauth"),
            ({"GITHUB_TOKEN": "g", "ANTHROPIC_API_KEY": "sk-x"}, "api_key"),
        ],
    )
    def test_audit_env_passed_matches_argv_env_flags(self, tmp_path, host_env, expected_mode):
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env=host_env)
        argv_env_names = [
            argv[i + 1] for i, t in enumerate(argv) if t == "--env" and i + 1 < len(argv)
        ]
        audit = build_cred_audit(
            host_env=host_env, home_dir=worker.home_dir, workdir=worker.workdir
        )
        assert audit.auth_mode == expected_mode
        # Both surfaces agree on the env-passthrough set.
        assert set(audit.env_vars_passed) == set(argv_env_names), (
            f"audit env_vars_passed ({audit.env_vars_passed!r}) disagrees "
            f"with argv --env flags ({argv_env_names!r})"
        )
