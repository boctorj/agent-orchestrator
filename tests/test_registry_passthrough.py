"""Internal-registry passthrough tests for F-001-U-4.

The unit description names six required behaviors:

  (a) ~/.npmrc / ~/.pip/pip.conf / ~/.docker/config.json present on host
      -> mounted read-only in the docker run argv.
  (b) absent -> not mounted (so workers without internal-registry config
      get an unchanged argv).
  (c) ORCH_WORKER_EXTRA_MOUNTS=foo,bar -> both mounted read-only.
  (d) Repo with package.json `"registry"` field but no passthrough wired
      -> doctor prints a warning.
  (e) Cred-audit output enumerates every auto-mounted path AND every
      internal-registry host pulled from ORCH_INTERNAL_REGISTRY_HOSTS.
  (f) NEVER_MOUNTED_HOST_PATHS in audit only lists paths that actually
      exist on the host (CRED AUDIT RECEIPT FIX folded from PR #11
      review SUGGESTION 3).

Subprocess is mocked; no real Docker daemon is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.workers.docker_claude_code import (
    AUTO_MOUNT_REGISTRY_PATHS,
    audit_registry_passthrough_for_repo,
    build_cred_audit,
)
from tests.conftest import _make_worker

# ---------------------------------------------------------------------------
# Helpers — mount-value extraction.
# ---------------------------------------------------------------------------


def _mount_values(argv: list[str]) -> list[str]:
    """Return every value that follows a `--mount` flag (in order)."""
    out: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--mount" and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def _has_ro_mount(argv: list[str], source: Path) -> bool:
    """True iff argv carries a bind mount with `source=<path>` AND `readonly`."""
    return any(f"source={source}" in value and "readonly" in value for value in _mount_values(argv))


# ---------------------------------------------------------------------------
# (a) Registry config present on host -> mounted ro.
# ---------------------------------------------------------------------------


class TestAutoMountWhenPresent:
    @pytest.mark.parametrize(
        ("rel_path", "container_path"),
        [
            (".npmrc", "/home/agent/.npmrc"),
            (".pip/pip.conf", "/home/agent/.pip/pip.conf"),
            (".docker/config.json", "/home/agent/.docker/config.json"),
        ],
    )
    def test_present_registry_config_is_mounted_readonly(
        self, tmp_path: Path, rel_path: str, container_path: str
    ) -> None:
        worker = _make_worker(tmp_path)
        host_path = worker.home_dir / rel_path
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text("# fake registry config")

        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        assert _has_ro_mount(argv, host_path), f"expected ro mount for {host_path}; argv={argv!r}"
        # Container-side target is the agent's HOME equivalent.
        mounts = [m for m in _mount_values(argv) if f"source={host_path}" in m]
        assert any(f"target={container_path}" in m for m in mounts), (
            f"expected container target {container_path}; got {mounts!r}"
        )

    def test_all_three_present_yields_three_extra_mounts(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        for rel in (".npmrc", ".pip/pip.conf", ".docker/config.json"):
            p = worker.home_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# fake")

        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        # Filter to just our three registry files.
        registry_mounts = [
            v
            for v in _mount_values(argv)
            if any(name in v for name in (".npmrc", "pip.conf", "config.json"))
        ]
        assert len(registry_mounts) == 3, (
            f"expected exactly 3 registry mounts; got {registry_mounts!r}"
        )
        for m in registry_mounts:
            assert "readonly" in m, f"registry mount must be ro: {m!r}"


# ---------------------------------------------------------------------------
# (b) Absent -> not mounted.
# ---------------------------------------------------------------------------


class TestAbsentPathsNotMounted:
    def test_no_registry_files_present_yields_no_registry_mounts(self, tmp_path: Path) -> None:
        """With a clean tmp home (no .npmrc/.pip/.docker), argv must not
        carry any of those mounts. Critical: workers without internal-
        registry config see an UNCHANGED argv vs the U-2 baseline."""
        worker = _make_worker(tmp_path)
        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        joined = " ".join(argv)
        # None of the three registry-config filenames should appear.
        for token in (".npmrc", "pip.conf", "config.json"):
            assert token not in joined, f"absent {token} must not be mounted; argv={argv!r}"

    def test_partial_presence_only_mounts_existing_files(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        # Only .npmrc exists; .pip and .docker do not.
        npmrc = worker.home_dir / ".npmrc"
        npmrc.write_text("# fake")

        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"})
        joined = " ".join(argv)
        assert str(npmrc) in joined, "present .npmrc must be mounted"
        assert "pip.conf" not in joined, "absent pip.conf must not be mounted"
        assert "config.json" not in joined, "absent docker config.json must not be mounted"


# ---------------------------------------------------------------------------
# (c) ORCH_WORKER_EXTRA_MOUNTS=foo,bar -> both mounted read-only.
# ---------------------------------------------------------------------------


class TestExtraMounts:
    def test_extra_mounts_env_var_mounts_each_path_readonly(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        foo = worker.home_dir / ".cargo" / "config.toml"
        bar = worker.home_dir / ".gemrc"
        foo.parent.mkdir(parents=True)
        foo.write_text("# fake")
        bar.write_text("# fake")

        argv = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={
                "GITHUB_TOKEN": "ghp_x",
                "ORCH_WORKER_EXTRA_MOUNTS": f"{foo},{bar}",
            },
        )
        assert _has_ro_mount(argv, foo), f"extra mount for {foo} missing; argv={argv!r}"
        assert _has_ro_mount(argv, bar), f"extra mount for {bar} missing; argv={argv!r}"

    def test_extra_mounts_silently_drops_nonexistent_entries(self, tmp_path: Path) -> None:
        """If a user lists a path that doesn't exist on the host, we
        must NOT try to bind-mount it (docker would error). Silently
        drop and rely on the cred-audit to surface what landed."""
        worker = _make_worker(tmp_path)
        real = worker.home_dir / ".gemrc"
        real.write_text("# fake")
        missing = worker.home_dir / ".not-there"

        argv = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={
                "GITHUB_TOKEN": "ghp_x",
                "ORCH_WORKER_EXTRA_MOUNTS": f"{real},{missing}",
            },
        )
        assert _has_ro_mount(argv, real)
        joined = " ".join(argv)
        assert ".not-there" not in joined, "missing extra mount must be dropped"

    def test_extra_mounts_tilde_expansion(self, tmp_path: Path) -> None:
        """`~/.foo` style entries must expand against the worker's
        configured `home_dir` (not the real $HOME — that would leak
        the developer's actual files into tests)."""
        worker = _make_worker(tmp_path)
        target = worker.home_dir / ".myrc"
        target.write_text("# fake")

        argv = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={
                "GITHUB_TOKEN": "ghp_x",
                "ORCH_WORKER_EXTRA_MOUNTS": "~/.myrc",
            },
        )
        assert _has_ro_mount(argv, target), (
            f"~/.myrc must expand under worker.home_dir; argv={argv!r}"
        )

    def test_extra_mounts_unset_no_op(self, tmp_path: Path) -> None:
        worker = _make_worker(tmp_path)
        argv_with = worker.build_docker_argv(
            ["claude", "-p", "x"],
            host_env={"GITHUB_TOKEN": "ghp_x", "ORCH_WORKER_EXTRA_MOUNTS": ""},
        )
        argv_without = worker.build_docker_argv(
            ["claude", "-p", "x"], host_env={"GITHUB_TOKEN": "ghp_x"}
        )
        assert argv_with == argv_without, "empty env var must not add mounts"


# ---------------------------------------------------------------------------
# (d) Repo with package.json `"registry"` but no passthrough -> doctor warn.
# ---------------------------------------------------------------------------


class TestRepoRegistryDetection:
    def test_package_json_with_registry_field_yields_warning(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(
            json.dumps({"name": "x", "registry": "https://artifactory.internal/repo/"})
        )

        warnings = audit_registry_passthrough_for_repo(
            repo,
            home_dir=tmp_path / "home",
            extra_mounts_env="",
        )
        assert warnings, "expected a warning when package.json has registry but no passthrough"
        joined = "\n".join(warnings).lower()
        assert "package.json" in joined and "registry" in joined

    def test_package_json_no_registry_field_no_warning(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({"name": "x", "dependencies": {}}))
        warnings = audit_registry_passthrough_for_repo(
            repo, home_dir=tmp_path / "home", extra_mounts_env=""
        )
        assert warnings == [], f"unexpected warnings for plain package.json: {warnings!r}"

    def test_requirements_txt_with_index_url_private_host_warns(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text(
            "--index-url https://internal-pypi.corp/simple\nrequests==2.31.0\n"
        )
        warnings = audit_registry_passthrough_for_repo(
            repo, home_dir=tmp_path / "home", extra_mounts_env=""
        )
        assert warnings, "expected a warning for requirements.txt --index-url"
        joined = "\n".join(warnings).lower()
        assert "requirements.txt" in joined

    def test_requirements_txt_with_index_url_pypi_does_not_warn(self, tmp_path: Path) -> None:
        """A bare `--index-url https://pypi.org/simple` is the public
        default; warning on it would be noisy."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("--index-url https://pypi.org/simple\nx\n")
        warnings = audit_registry_passthrough_for_repo(
            repo, home_dir=tmp_path / "home", extra_mounts_env=""
        )
        assert warnings == [], f"public pypi must not warn; got {warnings!r}"

    def test_passthrough_wired_via_npmrc_silences_warning(self, tmp_path: Path) -> None:
        """When the host has ~/.npmrc, the auto-mount wires the
        passthrough; no warning should fire even if package.json names a
        private registry."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(
            json.dumps({"registry": "https://artifactory.internal/"})
        )
        home = tmp_path / "home"
        home.mkdir()
        (home / ".npmrc").write_text("registry=https://artifactory.internal/\n")

        warnings = audit_registry_passthrough_for_repo(repo, home_dir=home, extra_mounts_env="")
        assert warnings == [], f"wired-passthrough must silence the warning: {warnings!r}"

    def test_passthrough_wired_via_extra_mounts_silences_warning(self, tmp_path: Path) -> None:
        """ORCH_WORKER_EXTRA_MOUNTS naming a path-that-exists-on-host
        also counts as 'passthrough wired' for the purpose of the
        doctor warning."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text(
            "--index-url https://internal-pypi.corp/simple\nrequests\n"
        )
        home = tmp_path / "home"
        home.mkdir()
        extra = home / ".my-pip-conf"
        extra.write_text("# user-configured passthrough")

        warnings = audit_registry_passthrough_for_repo(
            repo, home_dir=home, extra_mounts_env=str(extra)
        )
        assert warnings == [], f"explicit extra mount must silence warning: {warnings!r}"


# ---------------------------------------------------------------------------
# (e) cred-audit enumerates every auto-mounted path AND internal registry host.
# ---------------------------------------------------------------------------


class TestCredAuditSurfacesPassthrough:
    def test_audit_lists_each_auto_mounted_registry_file(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".npmrc").write_text("# fake")
        (home / ".pip").mkdir()
        (home / ".pip" / "pip.conf").write_text("# fake")
        (home / ".docker").mkdir()
        (home / ".docker" / "config.json").write_text("{}")

        audit = build_cred_audit(
            host_env={"GITHUB_TOKEN": "ghp_x"},
            home_dir=home,
            workdir=tmp_path,
        )
        rendered = audit.render()
        for fragment in (".npmrc", "pip.conf", "config.json"):
            assert fragment in rendered, (
                f"audit must enumerate auto-mounted {fragment}; got:\n{rendered}"
            )

    def test_audit_lists_internal_registry_hosts_when_env_set(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        audit = build_cred_audit(
            host_env={
                "GITHUB_TOKEN": "ghp_x",
                "ORCH_INTERNAL_REGISTRY_HOSTS": "artifactory.internal,internal-pypi.corp",
            },
            home_dir=home,
            workdir=tmp_path,
        )
        rendered = audit.render()
        for host in ("artifactory.internal", "internal-pypi.corp"):
            assert host in rendered, (
                f"audit must enumerate internal registry host {host!r}; got:\n{rendered}"
            )

    def test_audit_internal_registry_section_omitted_when_unset(self, tmp_path: Path) -> None:
        """No ORCH_INTERNAL_REGISTRY_HOSTS -> the audit must NOT show a
        misleading "Internal registry hosts: (none)" line that looks like
        a positive statement. Either omit the section entirely or render
        an unambiguous '(unset)'."""
        home = tmp_path / "home"
        home.mkdir()
        audit = build_cred_audit(
            host_env={"GITHUB_TOKEN": "ghp_x"},
            home_dir=home,
            workdir=tmp_path,
        )
        assert audit.internal_registry_hosts == (), (
            "no env var -> empty internal_registry_hosts tuple"
        )

    def test_audit_extra_mounts_listed_when_paths_exist(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        extra = home / ".gemrc"
        extra.write_text("# fake")
        audit = build_cred_audit(
            host_env={
                "GITHUB_TOKEN": "ghp_x",
                "ORCH_WORKER_EXTRA_MOUNTS": str(extra),
            },
            home_dir=home,
            workdir=tmp_path,
        )
        rendered = audit.render()
        assert ".gemrc" in rendered, f"audit must list extra mount; got:\n{rendered}"


# ---------------------------------------------------------------------------
# (f) NEVER_MOUNTED audit only lists paths that actually exist on host.
# CRED AUDIT RECEIPT FIX (PR #11 review SUGGESTION 3).
# ---------------------------------------------------------------------------


class TestNeverMountedExistenceFilter:
    def test_never_mounted_lists_only_existing_paths(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        # Create ~/.ssh but NOT ~/.aws or others.
        (home / ".ssh").mkdir()
        (home / ".gitconfig").write_text("[user]\n")

        audit = build_cred_audit(
            host_env={"GITHUB_TOKEN": "ghp_x"},
            home_dir=home,
            workdir=tmp_path,
        )
        # Only the existing paths show up; missing ones are filtered out.
        existing = set(audit.mounts_never)
        assert any(".ssh" in p for p in existing), (
            f"existing ~/.ssh should appear in mounts_never; got {existing!r}"
        )
        assert any(".gitconfig" in p for p in existing), (
            f"existing ~/.gitconfig should appear in mounts_never; got {existing!r}"
        )
        # Non-existent paths must not appear.
        assert not any(".aws" in p for p in existing), (
            f"~/.aws does not exist on host; must NOT appear: {existing!r}"
        )
        assert not any(".kube" in p for p in existing), (
            f"~/.kube does not exist on host; must NOT appear: {existing!r}"
        )

    def test_never_mounted_empty_when_no_paths_exist(self, tmp_path: Path) -> None:
        """Clean host (none of the never-mount paths exist) -> empty
        mounts_never AND a "(none present on host)" line in the rendered
        audit so the receipts heading isn't misleading."""
        home = tmp_path / "home"
        home.mkdir()
        audit = build_cred_audit(
            host_env={"GITHUB_TOKEN": "ghp_x"},
            home_dir=home,
            workdir=tmp_path,
        )
        assert audit.mounts_never == (), (
            f"clean home should yield empty mounts_never; got {audit.mounts_never!r}"
        )
        rendered = audit.render()
        # Either omit the heading entirely or follow it with an
        # explicit "(none present on host)" disclaimer.
        if "NEVER mounted" in rendered:
            assert "(none present on host)" in rendered, (
                f"NEVER-mounted heading must show '(none present on host)'; got:\n{rendered}"
            )


# ---------------------------------------------------------------------------
# Cross-cutting: registry paths shipped as a public constant.
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_auto_mount_registry_paths_constant_lists_three_files(self) -> None:
        assert set(AUTO_MOUNT_REGISTRY_PATHS) == {
            "~/.npmrc",
            "~/.pip/pip.conf",
            "~/.docker/config.json",
        }


# ---------------------------------------------------------------------------
# Sanity: registry mounts are still ro in BOTH auth modes.
# ---------------------------------------------------------------------------


class TestModeIndependence:
    @pytest.mark.parametrize(
        "host_env",
        [
            {"GITHUB_TOKEN": "ghp_x"},
            {"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-ant-x"},
        ],
        ids=["oauth", "api_key"],
    )
    def test_npmrc_mounted_in_both_modes(self, tmp_path: Path, host_env: dict[str, str]) -> None:
        worker = _make_worker(tmp_path)
        npmrc = worker.home_dir / ".npmrc"
        npmrc.write_text("# fake")
        argv = worker.build_docker_argv(["claude", "-p", "x"], host_env=host_env)
        assert _has_ro_mount(argv, npmrc), (
            f"registry passthrough must work in both modes; argv={argv!r}"
        )


# ---------------------------------------------------------------------------
# Worker re-export: AUTO_MOUNT_REGISTRY_PATHS must be importable from
# orchestrator.workers.docker_claude_code (callers in cli / tools reach it).
# ---------------------------------------------------------------------------


def test_auto_mount_registry_paths_is_re_exported_in_all() -> None:
    from orchestrator.workers import docker_claude_code

    assert "AUTO_MOUNT_REGISTRY_PATHS" in docker_claude_code.__all__


# Smoke-check the constructed worker uses os.environ when host_env is None.
def test_default_host_env_falls_back_to_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _make_worker(tmp_path)
    npmrc = worker.home_dir / ".npmrc"
    npmrc.write_text("# fake")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No explicit host_env — falls back to os.environ.
    argv = worker.build_docker_argv(["claude", "-p", "x"])
    assert _has_ro_mount(argv, npmrc)
