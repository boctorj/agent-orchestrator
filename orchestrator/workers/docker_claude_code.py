"""Docker + Claude Code implementation of the `Worker` protocol.

Runs each spawn/resume inside a locked-down `docker run` invocation. The
container image (`orchestrator/worker:latest`, built from
`docker/worker.Dockerfile`) bundles Python + Node + git + gh + the
`claude` CLI; this module is the host-side driver that calls
`subprocess.run(["docker", "run", ...])` with the hardening flags and
the auth-mode-dependent mounts.

See `docs/PROPOSAL-docker-workers.md` for the broader design. The salient
runtime invariants — every one of which has a regression test:

  * Strict credential boundary. Only `GITHUB_TOKEN` (always) and
    `ANTHROPIC_API_KEY` (API-key mode only) are forwarded into the
    container. The subprocess env handed to `docker run` is a curated
    dict, NOT `os.environ`. Random host vars (`AWS_*`, `SSH_*`, …) never
    appear in the rendered argv.
  * Hardening flags. `--cap-drop=ALL --security-opt=no-new-privileges
    --read-only --user 1000:1000 --memory=4g --cpus=2 --pids-limit=512`
    are baked into every invocation, alongside `--tmpfs` mounts for the
    writable paths the agent needs (`/tmp`, the npm/pip cache).
  * Auth-mode selection at spawn time:
      - `ANTHROPIC_API_KEY` set on host → API-key mode. Forwards the env
        var; does NOT mount `~/.claude`.
      - otherwise → OAuth mode. Mounts `~/.claude` read-only into the
        container; does NOT pass `ANTHROPIC_API_KEY`. The writable
        sessions sub-mount (`~/.claude/sessions`) is bound separately so
        `claude --resume` can persist new session state without writing
        through the read-only credentials mount.
  * Session continuity. Each spawn captures the session id from claude's
    JSON output; subsequent resumes pass `claude --resume <id>`.
    Persisting the session id in `state.db` is the caller's job; this
    module just surfaces it via the `Worker.spawn` return value.
  * DNS allowlist (F-001-U-3). Every container launches with
    `--dns=127.0.0.1 --dns-search=.`, pinning name resolution to the
    dnsmasq sidecar (`scripts/run-worker-dns.sh`). Allowlist lives in
    `orchestrator/network/allowlist.dnsmasq.conf`. The orch-net bridge
    network is checked by the 4th doctor probe so a missing network
    fails fast pre-flight instead of mid-spawn. SOFT BOUNDARY — raw-IP
    egress is still possible; see SECURITY.md "Non-defenses".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404 — invoking `docker` is the whole point
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Type alias for the subprocess.run-shaped callable that tests inject.
# Real `subprocess.run` has a complex overloaded signature; for our
# purposes we only need "called with argv + kwargs, returns something
# with .stdout/.stderr/.returncode".
SubprocessRunner = Callable[..., Any]
LogFn = Callable[[str], None]

# Default image tag built by `docker/worker.Dockerfile`. Override via
# `ORCH_DOCKER_WORKER_IMAGE` if you ship custom images per environment.
DEFAULT_IMAGE = "orchestrator/worker:latest"

# Custom bridge network the worker containers attach to. F-001-U-3 wires
# the dnsmasq sidecar in front of this network; `scripts/run-worker-dns.sh`
# is the idempotent creator.
DEFAULT_NETWORK = "orch-net"

# Loopback resolver every worker container is pinned to. The dnsmasq
# sidecar (scripts/run-worker-dns.sh) binds 127.0.0.1:5353 and answers
# only for hosts on the allowlist (orchestrator/network/allowlist.dnsmasq.conf);
# anything else resolves to 0.0.0.0 → outbound connect fails.
#
# SOFT BOUNDARY: this is name-level filtering. Raw-IP egress is still
# possible — a compromised worker that hardcodes 8.8.8.8 can still
# reach it. See SECURITY.md "Non-defenses".
DEFAULT_DNS = "127.0.0.1"
# Empty DNS search domain — stops glibc from appending `~/.local` or
# the host's search-path entries to bare names. Forces every query
# through dnsmasq verbatim.
DEFAULT_DNS_SEARCH = "."

# Host UID/GID the container runs as. Matches the `agent` user baked into
# the Dockerfile.
AGENT_UID = 1000
AGENT_GID = 1000

# Resource caps. Conservative — workers should not need more than this to
# clone, install deps, run tests, push a PR.
DEFAULT_MEMORY = "4g"
DEFAULT_CPUS = "2"
DEFAULT_PIDS_LIMIT = "512"

# Writable scratch mounts. Backed by tmpfs (not the host disk) so a
# compromised worker can scribble but not persist. The `/tmp` here is
# a *container-side* tmpfs target, not a host path — bandit's hardcoded-
# tmp warning doesn't apply (no host filesystem is touched).
TMPFS_TMP = "/tmp:size=512M,mode=1777"  # nosec B108
TMPFS_CACHE = "/home/agent/.cache:size=512M,mode=0700"

# Auth mode literal — kept as a small Enum-ish alias so call-sites can
# annotate without importing the typing machinery.
AuthMode = Literal["api_key", "oauth"]

# Env vars that NEVER cross the worker boundary, even if present on the
# host. The list is informational (the boundary is enforced positively —
# only the whitelist passes through) and is what `doctor` prints in the
# "WILL NOT receive" column. Keep in sync with `SECURITY.md`.
SENSITIVE_ENV_PREFIXES = (
    "AWS_",
    "SSH_",
    "GCP_",
    "GOOGLE_",
    "AZURE_",
    "KUBE",
    "DOCKER_",
)
SENSITIVE_ENV_NAMES = (
    "ANTHROPIC_API_KEY",  # never in OAuth mode; otherwise explicitly whitelisted
    "OPENAI_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "HOME",
    "USER",
    "PATH",
)

# Host paths the worker NEVER mounts into the container. Surfaced verbatim
# by `doctor` so the user can audit the boundary at a glance.
NEVER_MOUNTED_HOST_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.kube",
    "~/.gitconfig",
    "~/.git-credentials",
    "~/.npmrc",
    "~/.docker",
)


# ---------------------------------------------------------------------------
# Public helpers — session id extraction, auth-mode selection.
# ---------------------------------------------------------------------------


def select_auth_mode(host_env: dict[str, str] | None = None) -> AuthMode:
    """Choose API-key vs OAuth mode from the host environment.

    Pure function — no I/O, no filesystem touch. Callers pass `os.environ`
    (or a snapshot of it) so tests can drive both branches without monkey-
    patching the process env.
    """
    env = host_env if host_env is not None else os.environ
    return "api_key" if env.get("ANTHROPIC_API_KEY") else "oauth"


def extract_session_id(stdout: str) -> str | None:
    """Best-effort session-id extraction from `claude -p`'s output.

    Tries three formats, in order:
      1. Single-line JSON: `{"session_id": "...", ...}` — what
         `claude --output-format json` emits today.
      2. Streaming JSONL where one of the lines carries `session_id`.
      3. A loose plaintext regex `session[_-]?id: <id>` as a last resort
         for older CLI versions or non-JSON modes.

    Returns the id string or `None` if nothing matched. The worker
    treats `None` as "session lost; next call will create a fresh one".
    """
    text = stdout.strip()
    if not text:
        return None

    # (1) full-document JSON
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("session_id", "sessionId", "session"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val

    # (2) JSONL — scan each line independently
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            for key in ("session_id", "sessionId", "session"):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    return val

    # (3) plaintext fallback
    match = re.search(
        r"session[_-]?id[\"'\s:=]+([A-Za-z0-9][\w\-]{3,})",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Credential audit — used by `orchestrator doctor` and tests.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredAudit:
    """Structured view of the credential boundary for one host environment.

    Rendered as a fixed-format text block by `render()` so the doctor
    command shows the same receipts the snapshot test asserts on.
    """

    backend: str
    auth_mode: AuthMode
    image: str
    network: str
    env_vars_passed: tuple[str, ...]
    env_vars_dropped: tuple[str, ...]
    mounts_passed: tuple[str, ...]
    mounts_never: tuple[str, ...]

    def render(self) -> str:
        lines: list[str] = []
        lines.append("Worker backend: " + self.backend)
        auth_label = "API key" if self.auth_mode == "api_key" else "claude.ai OAuth"
        lines.append("Auth: " + auth_label)
        lines.append("Image: " + self.image)
        lines.append("Network: " + self.network)
        lines.append("")
        lines.append("Env vars passed into worker:")
        for name in self.env_vars_passed:
            lines.append(f"  + {name}")
        if not self.env_vars_passed:
            lines.append("  (none)")
        lines.append("")
        lines.append("Env vars dropped (host has them, worker will NOT receive):")
        for name in self.env_vars_dropped:
            lines.append(f"  - {name}")
        if not self.env_vars_dropped:
            lines.append("  (none present on host)")
        lines.append("")
        lines.append("Mounts into worker:")
        for entry in self.mounts_passed:
            lines.append(f"  + {entry}")
        lines.append("")
        lines.append("Paths NEVER mounted (host has them, worker will NOT see them):")
        for entry in self.mounts_never:
            lines.append(f"  - {entry}")
        return "\n".join(lines)


def _classify_dropped_env(host_env: dict[str, str], passed: tuple[str, ...]) -> tuple[str, ...]:
    """Return the host env var names that look sensitive AND aren't whitelisted.

    The result is sorted + de-duplicated so the audit string is stable
    across runs (snapshot tests would otherwise flap on dict ordering).
    """
    passed_set = set(passed)
    dropped: set[str] = set()
    for name in host_env:
        if name in passed_set:
            continue
        if name in SENSITIVE_ENV_NAMES:
            dropped.add(name)
            continue
        if any(name.startswith(prefix) for prefix in SENSITIVE_ENV_PREFIXES):
            dropped.add(name)
    return tuple(sorted(dropped))


def build_cred_audit(
    *,
    auth_mode: AuthMode | None = None,
    host_env: dict[str, str] | None = None,
    home_dir: Path | None = None,
    image: str = DEFAULT_IMAGE,
    network: str = DEFAULT_NETWORK,
    workdir: Path | None = None,
) -> CredAudit:
    """Build the structured credential audit for the docker worker.

    All inputs are explicit so the snapshot test can drive both auth
    modes deterministically. `host_env=None` falls back to `os.environ`
    so the CLI doctor command can call this without ceremony.
    """
    env = dict(host_env) if host_env is not None else dict(os.environ)
    mode: AuthMode = auth_mode if auth_mode is not None else select_auth_mode(env)
    home = home_dir if home_dir is not None else Path.home()
    wd = workdir if workdir is not None else Path.cwd()

    passed_env: list[str] = ["GITHUB_TOKEN"]
    if mode == "api_key":
        passed_env.append("ANTHROPIC_API_KEY")

    mounts_passed = [
        f"{wd} -> /workspace (rw)",
        f"{home / '.claude' / 'sessions'} -> /home/agent/.claude/sessions (rw)",
        "tmpfs -> /tmp (rw, 512M)",
        "tmpfs -> /home/agent/.cache (rw, 512M)",
    ]
    if mode == "oauth":
        mounts_passed.insert(
            1,
            f"{home / '.claude'} -> /home/agent/.claude (ro)",
        )

    return CredAudit(
        backend="docker",
        auth_mode=mode,
        image=image,
        network=network,
        env_vars_passed=tuple(passed_env),
        env_vars_dropped=_classify_dropped_env(env, tuple(passed_env)),
        mounts_passed=tuple(mounts_passed),
        mounts_never=NEVER_MOUNTED_HOST_PATHS,
    )


# ---------------------------------------------------------------------------
# Docker daemon / image / claude probes — used by the doctor command.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorProbeResult:
    """One outcome from `run_doctor_probes`. `ok=True` ⇒ check passed."""

    name: str
    ok: bool
    detail: str = ""


def run_doctor_probes(
    *,
    image: str = DEFAULT_IMAGE,
    network: str = DEFAULT_NETWORK,
    run: SubprocessRunner | None = None,
) -> list[DoctorProbeResult]:
    """Verify Docker daemon, image, claude CLI, and the orch-net bridge.

    `run` is an injectable `subprocess.run`-shaped callable so tests can
    avoid invoking the real Docker daemon. Production code uses the
    real `subprocess.run`.

    The 4th probe (orch-net network exists) is folded from PR #11
    reviewer feedback: without it `audit.render()` would print
    "Network: orch-net" like a fact but the actual spawn would error
    with "network orch-net not found". Plug that gap pre-flight.
    """
    runner: SubprocessRunner = run if run is not None else subprocess.run
    results: list[DoctorProbeResult] = []

    if not shutil.which("docker"):
        results.append(
            DoctorProbeResult(
                "docker CLI on PATH", False, "`docker` not found — install Docker Desktop / Engine"
            )
        )
        return results
    results.append(DoctorProbeResult("docker CLI on PATH", True))

    # 1) Daemon reachable
    try:
        proc = runner(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            results.append(DoctorProbeResult("docker daemon reachable", True, proc.stdout.strip()))
        else:
            results.append(
                DoctorProbeResult(
                    "docker daemon reachable",
                    False,
                    (proc.stderr or proc.stdout or "non-zero exit").strip(),
                )
            )
            return results
    except Exception as exc:  # noqa: BLE001
        results.append(DoctorProbeResult("docker daemon reachable", False, str(exc)))
        return results

    # 2) Worker image present
    try:
        proc = runner(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            digest = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "<unknown>"
            results.append(DoctorProbeResult(f"image {image} built", True, digest[:23]))
        else:
            results.append(
                DoctorProbeResult(
                    f"image {image} built",
                    False,
                    f"build with: docker build -f docker/worker.Dockerfile -t {image} .",
                )
            )
            return results
    except Exception as exc:  # noqa: BLE001
        results.append(DoctorProbeResult(f"image {image} built", False, str(exc)))
        return results

    # 3) claude CLI works inside the container — run `claude --version`
    try:
        proc = runner(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint=claude",
                image,
                "--version",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            results.append(
                DoctorProbeResult(
                    "claude --version inside container",
                    True,
                    proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "ok",
                )
            )
        else:
            results.append(
                DoctorProbeResult(
                    "claude --version inside container",
                    False,
                    (proc.stderr or proc.stdout or "non-zero exit").strip(),
                )
            )
    except Exception as exc:  # noqa: BLE001
        results.append(DoctorProbeResult("claude --version inside container", False, str(exc)))

    # 4) orch-net bridge network exists. Folded from PR #11 reviewer
    # SUGGESTION 2: a missing network is the most-common spawn-time
    # failure mode for users who skipped `scripts/run-worker-dns.sh`,
    # and the audit's "Network: orch-net" line reads like a guarantee.
    # Probe surfaces the gap pre-flight with a fix-it hint.
    probe_name = f"network {network} exists"
    try:
        proc = runner(
            ["docker", "network", "inspect", network, "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            results.append(DoctorProbeResult(probe_name, True, proc.stdout.strip() or network))
        else:
            results.append(
                DoctorProbeResult(
                    probe_name,
                    False,
                    (
                        f"create with: docker network create {network} --driver bridge "
                        f"(or run: scripts/run-worker-dns.sh)"
                    ),
                )
            )
    except Exception as exc:  # noqa: BLE001
        results.append(DoctorProbeResult(probe_name, False, str(exc)))
    return results


# ---------------------------------------------------------------------------
# DockerClaudeCodeWorker — the Worker implementation.
# ---------------------------------------------------------------------------


@dataclass
class DockerClaudeCodeWorker:
    """`Worker` backend that drives `claude` inside a hardened container.

    All `docker run` flags are derived deterministically from this
    object's attributes — `build_docker_argv()` is the single source of
    truth and is what the unit tests assert on.
    """

    role: str
    workdir: Path = field(default_factory=Path.cwd)
    home_dir: Path = field(default_factory=Path.home)
    image: str = DEFAULT_IMAGE
    network: str = DEFAULT_NETWORK
    dns: str = DEFAULT_DNS
    dns_search: str = DEFAULT_DNS_SEARCH
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    pids_limit: str = DEFAULT_PIDS_LIMIT
    user: str = f"{AGENT_UID}:{AGENT_GID}"
    # Optional dependency-injection seam for tests. Defaults to the real
    # subprocess.run on construction (avoids referencing it at class-decl
    # time, which would break monkeypatching of `subprocess.run` itself).
    run: SubprocessRunner | None = None
    # Optional logger callback — accepts a single string. Lets the CLI
    # log "Auth: API key" / "Auth: claude.ai OAuth" at spawn without
    # this module depending on rich/Click.
    log: LogFn | None = None

    def _runner(self) -> SubprocessRunner:
        return self.run if self.run is not None else subprocess.run

    def _emit(self, msg: str) -> None:
        if self.log is None:
            return
        # Logging must never crash a spawn; suppress all logger failures.
        import contextlib

        with contextlib.suppress(Exception):
            self.log(msg)

    def auth_mode(self, host_env: dict[str, str] | None = None) -> AuthMode:
        return select_auth_mode(host_env)

    # ----- argv construction (the security-critical bit) -----------------

    def build_docker_argv(
        self,
        claude_argv: list[str],
        *,
        host_env: dict[str, str] | None = None,
        container_name: str | None = None,
    ) -> list[str]:
        """Render the full `docker run ...` argv for one invocation.

        `claude_argv` is the command to run *inside* the container —
        typically `["claude", "-p", task]` for `spawn` or
        `["claude", "--resume", sid, "-p", msg]` for `resume`. The
        returned list is exactly what `subprocess.run` is called with;
        the rendered shape is what the unit tests inspect.
        """
        mode = self.auth_mode(host_env)
        argv: list[str] = [
            "docker",
            "run",
            "--rm",
            # ----- hardening flags -----
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            f"--tmpfs={TMPFS_TMP}",
            f"--tmpfs={TMPFS_CACHE}",
            "--user",
            self.user,
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            f"--network={self.network}",
            # ----- DNS allowlist (F-001-U-3) -----
            # Pin every name resolution to the dnsmasq sidecar that
            # enforces the worker allowlist. Raw-IP egress is still
            # possible; see DEFAULT_DNS doc + SECURITY.md.
            f"--dns={self.dns}",
            f"--dns-search={self.dns_search}",
        ]
        if container_name:
            argv += ["--name", container_name]

        # ----- mounts (rw workspace + writable session dir) -----
        argv += [
            "--mount",
            f"type=bind,source={self.workdir},target=/workspace",
        ]
        sessions_dir = self.home_dir / ".claude" / "sessions"
        argv += [
            "--mount",
            (f"type=bind,source={sessions_dir},target=/home/agent/.claude/sessions"),
        ]

        # ----- auth-mode-specific mount + env -----
        if mode == "oauth":
            claude_dir = self.home_dir / ".claude"
            argv += [
                "--mount",
                (f"type=bind,source={claude_dir},target=/home/agent/.claude,readonly"),
            ]
        # Strict whitelist for env passthrough. We use the value-less
        # `--env NAME` form (docker reads it from the curated subprocess
        # env we hand it via `build_subprocess_env`). This keeps the
        # actual secret out of argv so it can't leak through `ps`.
        argv += ["--env", "GITHUB_TOKEN"]
        if mode == "api_key":
            argv += ["--env", "ANTHROPIC_API_KEY"]

        # ----- image + command -----
        argv.append(self.image)
        argv.extend(claude_argv)
        return argv

    def build_subprocess_env(self, host_env: dict[str, str] | None = None) -> dict[str, str]:
        """Curated env dict handed to `subprocess.run`.

        Critically NOT a copy of `os.environ`. Only the whitelisted
        names are forwarded plus the minimum to run `docker` itself
        (PATH, locale). Anything else from the host env is dropped on
        the floor — the same boundary the docker `--env` flags enforce
        on the container side.
        """
        src: Mapping[str, str] = host_env if host_env is not None else dict(os.environ)
        env: dict[str, str] = {}
        # docker CLI needs to find itself + its config; preserve enough
        # of the host env for that without leaking app secrets.
        for safe in ("PATH", "HOME", "LANG", "LC_ALL", "DOCKER_HOST"):
            if safe in src and src[safe]:
                env[safe] = src[safe]
        mode = select_auth_mode(dict(src))
        if "GITHUB_TOKEN" in src:
            env["GITHUB_TOKEN"] = src["GITHUB_TOKEN"]
        if mode == "api_key" and "ANTHROPIC_API_KEY" in src:
            env["ANTHROPIC_API_KEY"] = src["ANTHROPIC_API_KEY"]
        return env

    # ----- Worker protocol --------------------------------------------------

    def spawn(self, task: str, *, title: str | None = None) -> tuple[str, str]:
        """Launch a fresh `claude -p` container; return (session_id, response)."""
        mode = self.auth_mode()
        self._emit("Auth: API key" if mode == "api_key" else "Auth: claude.ai OAuth")
        claude_cmd = ["claude", "--output-format", "json", "-p", task]
        argv = self.build_docker_argv(claude_cmd)
        env = self.build_subprocess_env()
        runner = self._runner()
        proc = runner(
            argv,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
        stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
        returncode = getattr(proc, "returncode", 0)
        if returncode != 0:
            raise RuntimeError(
                f"docker run failed (exit {returncode}) for {self.role} spawn: "
                f"{(stderr or stdout).strip()[:500]}"
            )
        session_id = extract_session_id(stdout)
        if session_id is None:
            raise RuntimeError(
                f"Could not extract session_id from claude output for {self.role} spawn. "
                f"stdout head: {stdout[:200]!r}"
            )
        response = _extract_response_text(stdout)
        return session_id, response

    def resume(self, session_id: str, msg: str) -> str:
        """Send a follow-up to an existing session; return response text."""
        mode = self.auth_mode()
        self._emit("Auth: API key" if mode == "api_key" else "Auth: claude.ai OAuth")
        claude_cmd = [
            "claude",
            "--output-format",
            "json",
            "--resume",
            session_id,
            "-p",
            msg,
        ]
        argv = self.build_docker_argv(claude_cmd)
        env = self.build_subprocess_env()
        runner = self._runner()
        proc = runner(
            argv,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        stdout = (proc.stdout or "") if hasattr(proc, "stdout") else ""
        stderr = (proc.stderr or "") if hasattr(proc, "stderr") else ""
        returncode = getattr(proc, "returncode", 0)
        if returncode != 0:
            raise RuntimeError(
                f"docker run failed (exit {returncode}) for {self.role} resume: "
                f"{(stderr or stdout).strip()[:500]}"
            )
        return _extract_response_text(stdout)

    def archive(self, session_id: str) -> None:
        """No-op for the docker backend.

        Claude Code session files live in the bind-mounted
        `~/.claude/sessions` directory; they're cheap to retain and the
        host's own retention policy (or `~/.claude/sessions` housekeeping)
        is the right place to prune them, not this driver. Defined for
        Worker-protocol completeness.
        """
        return None


def _extract_response_text(stdout: str) -> str:
    """Pull the assistant's final response text out of claude's stdout.

    Mirrors `extract_session_id`: tries JSON first, then JSONL, then
    falls back to returning stdout verbatim. The shape mirrors
    `claude --output-format json`'s `result` field.
    """
    text = stdout.strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("result", "response", "text", "content"):
            val = data.get(key)
            if isinstance(val, str):
                return val
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            for key in ("result", "response", "text", "content"):
                val = obj.get(key)
                if isinstance(val, str):
                    return val
    return text


__all__ = [
    "AGENT_GID",
    "AGENT_UID",
    "DEFAULT_CPUS",
    "DEFAULT_DNS",
    "DEFAULT_DNS_SEARCH",
    "DEFAULT_IMAGE",
    "DEFAULT_MEMORY",
    "DEFAULT_NETWORK",
    "DEFAULT_PIDS_LIMIT",
    "NEVER_MOUNTED_HOST_PATHS",
    "SENSITIVE_ENV_NAMES",
    "SENSITIVE_ENV_PREFIXES",
    "AuthMode",
    "CredAudit",
    "DockerClaudeCodeWorker",
    "DoctorProbeResult",
    "build_cred_audit",
    "extract_session_id",
    "run_doctor_probes",
    "select_auth_mode",
]
