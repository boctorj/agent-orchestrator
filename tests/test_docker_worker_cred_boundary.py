"""Strict-credential-boundary tests for the Docker worker.

The unit description is unambiguous: "Env vars passed: `GITHUB_TOKEN`
(always), `ANTHROPIC_API_KEY` (API-key mode only). Everything else from
host env DROPPED." This file is the regression test for that promise.

Parameterized over a host env containing AWS_*, SSH_*, and a smattering
of other secret-shaped names; asserts NONE of them appear in the
rendered `docker run` argv. The subprocess env we hand to Docker must
also be a curated subset of the host env — verified separately.
"""

from __future__ import annotations

import pytest

from tests.conftest import _make_worker

# A representative "hostile" host environment: a mix of cloud SDK creds,
# SSH agent sockets, kube config pointers, generic API tokens, and random
# user-defined vars. None of these names should leak into the worker.
_HOSTILE_HOST_ENV_NAMES = [
    # AWS
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    # SSH
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SSH_CLIENT",
    # GCP / Google
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GCP_PROJECT",
    # Azure
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    # Kubernetes
    "KUBECONFIG",
    # Other LLM keys we'd never forward to a Claude worker
    "OPENAI_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    # GitHub App private key (PAT-only auth path)
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    # Random user env
    "EDITOR",
    "MY_SECRET_FOR_TESTS",
    "PERSONAL_DEV_TOKEN",
]


def _build_hostile_host_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {name: f"value-for-{name}" for name in _HOSTILE_HOST_ENV_NAMES}
    if extra:
        env.update(extra)
    return env


# `_make_worker` is imported from `tests/conftest.py` — see top of this file.


# ---------------------------------------------------------------------------
# Top-level invariant: argv must not contain any hostile names, in either mode.
# Parameterized so a regression in either branch flags as a distinct failure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("auth_extra", "label"),
    [
        ({"ANTHROPIC_API_KEY": "sk-ant-test", "GITHUB_TOKEN": "ghp_x"}, "api_key"),
        ({"GITHUB_TOKEN": "ghp_x"}, "oauth"),
    ],
)
@pytest.mark.parametrize("hostile_var", _HOSTILE_HOST_ENV_NAMES)
def test_hostile_var_never_appears_in_docker_argv(tmp_path, hostile_var, auth_extra, label):
    """For EVERY hostile var and BOTH auth modes, the var must not appear
    in the rendered `docker run` argv (neither as `--env NAME` nor as a
    string anywhere in the argv list).
    """
    worker = _make_worker(tmp_path)
    argv = worker.build_docker_argv(
        ["claude", "-p", "hello"],
        host_env=_build_hostile_host_env(auth_extra),
    )

    # Concatenate argv into a single haystack — catches both
    # `--env AWS_ACCESS_KEY_ID` and any sneaky inline `KEY=value` form.
    joined = " ".join(argv)
    assert hostile_var not in joined, (
        f"[{label}] hostile env var {hostile_var!r} leaked into docker argv: {argv!r}"
    )
    # And its value certainly must not leak either.
    assert f"value-for-{hostile_var}" not in joined, (
        f"[{label}] value of hostile var {hostile_var!r} leaked into argv"
    )


# ---------------------------------------------------------------------------
# Whitelist enforcement: ONLY the expected names appear in argv after --env.
# ---------------------------------------------------------------------------


def _env_flag_values(argv: list[str]) -> list[str]:
    """Collect every value that follows a `--env` flag in argv."""
    values: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--env" and i + 1 < len(argv):
            values.append(argv[i + 1])
    return values


def test_oauth_mode_env_whitelist_is_exactly_github_token(tmp_path):
    worker = _make_worker(tmp_path)
    argv = worker.build_docker_argv(
        ["claude", "-p", "x"],
        host_env=_build_hostile_host_env({"GITHUB_TOKEN": "ghp_x"}),
    )
    assert _env_flag_values(argv) == ["GITHUB_TOKEN"]


def test_api_key_mode_env_whitelist_is_github_token_and_anthropic(tmp_path):
    worker = _make_worker(tmp_path)
    argv = worker.build_docker_argv(
        ["claude", "-p", "x"],
        host_env=_build_hostile_host_env(
            {"ANTHROPIC_API_KEY": "sk-ant-x", "GITHUB_TOKEN": "ghp_x"}
        ),
    )
    # Order matters here: GITHUB_TOKEN always first, ANTHROPIC_API_KEY second.
    # That's the contract the build_docker_argv method commits to.
    assert _env_flag_values(argv) == ["GITHUB_TOKEN", "ANTHROPIC_API_KEY"]


# ---------------------------------------------------------------------------
# The OTHER half of the boundary: the env dict handed to subprocess.run.
# argv-level enforcement alone isn't enough — if we ran the subprocess
# with `env=os.environ`, every hostile var would be visible to docker's
# `--env NAME` passthrough form. Verify the subprocess env is curated.
# ---------------------------------------------------------------------------


def test_subprocess_env_drops_hostile_vars_oauth(tmp_path):
    worker = _make_worker(tmp_path)
    env = worker.build_subprocess_env(host_env=_build_hostile_host_env({"GITHUB_TOKEN": "ghp_x"}))
    for hostile in _HOSTILE_HOST_ENV_NAMES:
        assert hostile not in env, (
            f"OAuth subprocess env should NOT carry {hostile!r}; got: {sorted(env)!r}"
        )
    assert env.get("GITHUB_TOKEN") == "ghp_x"
    assert "ANTHROPIC_API_KEY" not in env


def test_subprocess_env_drops_hostile_vars_api_key(tmp_path):
    worker = _make_worker(tmp_path)
    env = worker.build_subprocess_env(
        host_env=_build_hostile_host_env({"GITHUB_TOKEN": "ghp_x", "ANTHROPIC_API_KEY": "sk-ant-x"})
    )
    for hostile in _HOSTILE_HOST_ENV_NAMES:
        assert hostile not in env
    assert env.get("GITHUB_TOKEN") == "ghp_x"
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-x"


# ---------------------------------------------------------------------------
# Mount boundary: hostile host paths must never be added as bind mounts.
# ---------------------------------------------------------------------------


_HOSTILE_PATHS = (
    "/.ssh",
    "/.aws",
    "/.config/gcloud",
    "/.kube",
    "/.gitconfig",
    "/.git-credentials",
)


@pytest.mark.parametrize("path_fragment", _HOSTILE_PATHS)
@pytest.mark.parametrize(
    "auth_extra",
    [
        {"ANTHROPIC_API_KEY": "sk-ant-x", "GITHUB_TOKEN": "ghp_x"},
        {"GITHUB_TOKEN": "ghp_x"},
    ],
    ids=["api_key", "oauth"],
)
def test_no_hostile_path_is_mounted(tmp_path, path_fragment, auth_extra):
    worker = _make_worker(tmp_path)
    argv = worker.build_docker_argv(
        ["claude", "-p", "x"],
        host_env=_build_hostile_host_env(auth_extra),
    )
    for i, tok in enumerate(argv):
        if tok == "--mount" and i + 1 < len(argv):
            assert path_fragment not in argv[i + 1], (
                f"hostile path fragment {path_fragment!r} appeared in mount: {argv[i + 1]!r}"
            )
        if tok == "-v" and i + 1 < len(argv):
            assert path_fragment not in argv[i + 1], (
                f"hostile path fragment {path_fragment!r} appeared in -v mount: {argv[i + 1]!r}"
            )
