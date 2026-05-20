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

from orchestrator.workers.base import TailMessage, TailResult, TailStatus, _validate_limit

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

# Host paths the worker NEVER mounts into the container. Surfaced by
# `doctor` so the user can audit the boundary at a glance.
#
# CRED AUDIT RECEIPT FIX (F-001-U-4, folded from PR #11 review
# SUGGESTION 3): the audit only lists paths from this tuple that
# actually exist on the host, so the "host has them" qualifier in the
# rendered heading is accurate.
#
# Note: ~/.npmrc and ~/.docker — previously listed here in U-2 — moved
# to AUTO_MOUNT_REGISTRY_PATHS below in U-4. They are now mounted
# read-only when present so internal-registry passthrough works
# out-of-the-box. See AUTO_MOUNT_REGISTRY_PATHS for the rationale.
NEVER_MOUNTED_HOST_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.kube",
    "~/.gitconfig",
    "~/.git-credentials",
)

# Internal-registry config files auto-mounted (read-only) into the
# worker container WHEN they exist on the host. No flag needed — the
# default-on behavior makes `npm install`, `pip install`, and
# `docker pull` against an internal artifactory / private PyPI /
# private container registry "just work" from inside the worker.
#
# Read-only: workers never need to mutate these files. Read-write
# would also broaden the blast radius if a worker is compromised
# (it could rewrite the user's auth config and have it persist
# across sessions).
#
# Runtime bind-mount semantics (PR #14 review C2): at container start,
# the host file is bind-mounted verbatim — the container sees an exact
# read-only view of whatever's on the host at that path, evaluated
# fresh on every spawn. There's no build-time copy; the worker image
# itself ships no registry credentials. If the host file is missing,
# the mount is silently skipped (see `_auto_mounted_registry_paths`).
# Container-side mount target mirrors the same relative path under
# /home/agent/, since the agent UID matches the in-container HOME.
#
# Blast radius (PR #14 review C1): these files commonly hold bearer
# tokens — `_authToken=…` in .npmrc, `auths.<host>.auth` base64-creds
# in .docker/config.json, and pip `extra-index-url=https://user:pass@…`
# in pip.conf. The worker container can READ them — necessary for
# `npm install` against an internal registry to authenticate — and
# can therefore exfiltrate them within the limits of the dnsmasq DNS
# allowlist (raw-IP egress remains possible; see SECURITY.md). Trust
# model: the agent runs at the same trust level as the user invoking
# the orchestrator. Mounting these files into the worker is no broader
# than the user running `npm install` themselves; it just removes the
# need for the user to paste credentials into a worker-specific config.
# Read-only enforcement plus the cred-audit receipts (see CredAudit.
# render — it enumerates every mounted path so the user can see what
# crossed the boundary on each spawn) are the layered defenses here.
AUTO_MOUNT_REGISTRY_PATHS: tuple[str, ...] = (
    "~/.npmrc",
    "~/.pip/pip.conf",
    "~/.docker/config.json",
)

# Env var names that the worker consults for additional passthrough.
# Both are comma-separated lists of values, parsed at argv-build time.
# See `_parse_extra_mounts` / `_parse_internal_registry_hosts`.
EXTRA_MOUNTS_ENV = "ORCH_WORKER_EXTRA_MOUNTS"
INTERNAL_REGISTRY_HOSTS_ENV = "ORCH_INTERNAL_REGISTRY_HOSTS"

# Subprocess timeout knob for spawn() / resume(). Default 30 min: long
# enough for a worker to clone, install deps, run tests, push a PR;
# short enough to catch a deadlock without burning a full CI budget.
# Override via the ORCH_WORKER_TIMEOUT_SECONDS env knob (or pass
# `timeout_seconds=` to `DockerClaudeCodeWorker`).
#
# F-001-U-6 (folded from PR #11 reviewer SUGGESTION 1): without this the
# orchestrator thread blocks indefinitely on a hung `docker run`. The
# doctor probes already use 10s/30s timeouts; this closes the parallel
# gap on the real spawn/resume path.
DEFAULT_SPAWN_TIMEOUT_SECONDS = 30 * 60
TIMEOUT_ENV = "ORCH_WORKER_TIMEOUT_SECONDS"


def _resolve_timeout_seconds(
    override: int | None,
    host_env: Mapping[str, str] | None = None,
) -> int:
    """Pick the effective subprocess timeout for one spawn/resume call.

    Resolution order:
      1. Explicit `timeout_seconds=` constructor field on the worker
         (wins outright when set; tests use this to drive 10s timeouts).
      2. `ORCH_WORKER_TIMEOUT_SECONDS` env knob (positive integer).
      3. `DEFAULT_SPAWN_TIMEOUT_SECONDS` (30 min).

    A non-positive or unparseable env value is silently ignored: fall
    through to the default rather than crash a real spawn over a typo.
    """
    if override is not None and override > 0:
        return override
    src: Mapping[str, str] = host_env if host_env is not None else os.environ
    raw = src.get(TIMEOUT_ENV, "")
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_SPAWN_TIMEOUT_SECONDS
        if val > 0:
            return val
    return DEFAULT_SPAWN_TIMEOUT_SECONDS


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


def _expand_with_home(path_str: str, home: Path) -> Path:
    """Expand a `~/...` path against the supplied `home` (NOT $HOME).

    Tests rely on a tmp home_dir, so we can't use `Path.expanduser()`
    which reads the process's $HOME. Falls through to a plain `Path`
    for absolute / non-tilde inputs.

    Portability (PR #14 review C3): the expansion itself is OS-agnostic
    — `Path` normalizes separators on every platform. What's NOT
    OS-agnostic is the *content* of `AUTO_MOUNT_REGISTRY_PATHS`, which
    encodes Unix conventions: `~/.npmrc`, `~/.pip/pip.conf`, and
    `~/.docker/config.json` exist by default on macOS/Linux but live
    under `%APPDATA%` on Windows (e.g. `%APPDATA%\\npm\\etc\\npmrc`).
    On Windows the auto-mount tuple typically yields zero matches; the
    user wires passthrough explicitly via `ORCH_WORKER_EXTRA_MOUNTS`
    pointing at the actual Windows config paths. Docker Desktop on
    Windows then bind-mounts those into the Linux container at the
    `/home/agent/...` target like any other extra mount.
    """
    if path_str.startswith("~/"):
        return home / path_str[2:]
    if path_str == "~":
        return home
    return Path(path_str)


def _auto_mounted_registry_paths(home: Path) -> list[tuple[Path, str]]:
    """Return the (host_path, container_path) pairs for every registry
    config file that exists on the host.

    The container target mirrors the relative path under the agent's
    HOME (/home/agent/) since the user IDs match.
    """
    pairs: list[tuple[Path, str]] = []
    for spec in AUTO_MOUNT_REGISTRY_PATHS:
        # spec is always "~/<rel>" — the canonical form.
        if not spec.startswith("~/"):
            continue
        rel = spec[2:]
        host_path = home / rel
        if host_path.is_file():
            container_path = f"/home/agent/{rel}"
            pairs.append((host_path, container_path))
    return pairs


def _violates_never_mount(candidate: Path, home: Path) -> str | None:
    """Return the NEVER_MOUNT entry the candidate matches, or None.

    A candidate "matches" if it resolves to, or sits under, the resolved
    path of a NEVER_MOUNTED_HOST_PATHS entry. Symlinks are resolved on
    both sides so a symlink that points into ~/.ssh can't slip past the
    check. Non-existent NEVER paths can't match (they're not on this
    host, so there's nothing to protect).
    """
    try:
        candidate_abs = candidate.resolve()
    except (OSError, RuntimeError):
        candidate_abs = candidate.absolute()
    for spec in NEVER_MOUNTED_HOST_PATHS:
        never = _expand_with_home(spec, home)
        try:
            never_abs = never.resolve()
        except (OSError, RuntimeError):
            never_abs = never.absolute()
        if candidate_abs == never_abs:
            return spec
        # `is_relative_to` is the prefix-containment check that handles
        # `~/.ssh/id_rsa` matching `~/.ssh`. Python 3.9+ has it on Path.
        try:
            if candidate_abs.is_relative_to(never_abs):
                return spec
        except AttributeError:  # pragma: no cover — < 3.9 not supported
            if str(candidate_abs).startswith(str(never_abs) + "/"):
                return spec
    return None


def _parse_extra_mounts(env: Mapping[str, str], home: Path) -> list[Path]:
    """Parse ORCH_WORKER_EXTRA_MOUNTS into a list of existing host paths.

    Comma-separated; whitespace-trimmed; `~/` expanded against `home`.
    Non-existent entries are silently dropped — docker would refuse to
    bind-mount them and they'd just produce noise. The cred-audit
    surface is responsible for telling the user what landed.

    Safety check (PR #14 review C4): every entry is validated against
    NEVER_MOUNTED_HOST_PATHS. A candidate that resolves to or under a
    NEVER entry (`~/.ssh`, `~/.aws`, …) is REJECTED with `ValueError`
    rather than silently dropped — a NEVER-list violation is a security
    failure, not a typo, so the worker refuses to spawn until the env
    var is fixed.
    """
    raw = env.get(EXTRA_MOUNTS_ENV, "")
    if not raw:
        return []
    out: list[Path] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        candidate = _expand_with_home(item, home)
        violated = _violates_never_mount(candidate, home)
        if violated is not None:
            raise ValueError(
                f"{EXTRA_MOUNTS_ENV} entry {item!r} resolves to or under "
                f"the never-mounted path {violated!r} — refusing to bind-"
                f"mount this into the worker. Remove the entry from "
                f"{EXTRA_MOUNTS_ENV} or pick a path outside the never-list."
            )
        if candidate.exists():
            out.append(candidate)
    return out


def _parse_internal_registry_hosts(env: Mapping[str, str]) -> tuple[str, ...]:
    """Parse ORCH_INTERNAL_REGISTRY_HOSTS into a tuple of hostnames.

    Comma-separated; whitespace-trimmed; empty entries dropped. Sorted
    so the rendered audit is stable across runs.
    """
    raw = env.get(INTERNAL_REGISTRY_HOSTS_ENV, "")
    if not raw:
        return ()
    hosts = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    return tuple(sorted(set(hosts)))


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

    Field additions in F-001-U-4:

      * ``mounts_never`` is filtered by host-side existence — the
        rendered "host has them, worker will NOT see them" heading is
        now accurate (PR #11 review SUGGESTION 3).
      * ``internal_registry_hosts`` lists every hostname forwarded
        through the dnsmasq allowlist via ORCH_INTERNAL_REGISTRY_HOSTS
        so the user sees what crossed the named-host boundary.
    """

    backend: str
    auth_mode: AuthMode
    image: str
    network: str
    env_vars_passed: tuple[str, ...]
    env_vars_dropped: tuple[str, ...]
    mounts_passed: tuple[str, ...]
    mounts_never: tuple[str, ...]
    # F-001-U-4 additions — kept at the end for backwards-compat with
    # existing tests that construct CredAudit positionally via dataclass
    # default ordering. New code should pass by keyword.
    internal_registry_hosts: tuple[str, ...] = ()

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
        if not self.mounts_never:
            lines.append("  (none present on host)")
        # F-001-U-4: internal-registry DNS allowlist additions. Section
        # is OMITTED entirely when the env var is unset (no misleading
        # "(none)" line that reads like a positive statement).
        if self.internal_registry_hosts:
            lines.append("")
            lines.append("Internal registry hosts (added to DNS allowlist):")
            for host in self.internal_registry_hosts:
                lines.append(f"  + {host}")
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

    # F-001-U-4: surface every auto-mounted registry config file so the
    # cred-audit accurately reflects what crossed the boundary.
    for host_path, container_path in _auto_mounted_registry_paths(home):
        mounts_passed.append(f"{host_path} -> {container_path} (ro, auto)")
    for extra in _parse_extra_mounts(env, home):
        # User-approved opt-in via ORCH_WORKER_EXTRA_MOUNTS. Mirrors the
        # mount target naming we use in argv (homed under /home/agent).
        target = _container_target_for_extra(extra, home)
        mounts_passed.append(f"{extra} -> {target} (ro, extra)")

    # F-001-U-4 (PR #11 SUGGESTION 3): only list NEVER paths that
    # actually exist on the host so the "host has them" heading is
    # accurate. Same predicate as `_classify_dropped_env`.
    mounts_never = tuple(
        spec for spec in NEVER_MOUNTED_HOST_PATHS if _expand_with_home(spec, home).exists()
    )

    internal_registry_hosts = _parse_internal_registry_hosts(env)

    return CredAudit(
        backend="docker",
        auth_mode=mode,
        image=image,
        network=network,
        env_vars_passed=tuple(passed_env),
        env_vars_dropped=_classify_dropped_env(env, tuple(passed_env)),
        mounts_passed=tuple(mounts_passed),
        mounts_never=mounts_never,
        internal_registry_hosts=internal_registry_hosts,
    )


def _container_target_for_extra(host_path: Path, home: Path) -> str:
    """Compute the container-side path for an extra-mount host file.

    If the host path lives under `home`, mirror the relative path under
    `/home/agent/`. Otherwise mount at `/mnt/extra/<basename>` to keep
    the user-approved opt-in confined to a predictable target.
    """
    try:
        rel = host_path.relative_to(home)
    except ValueError:
        return f"/mnt/extra/{host_path.name}"
    return f"/home/agent/{rel}"


# ---------------------------------------------------------------------------
# Registry-passthrough doctor probe (F-001-U-4).
# ---------------------------------------------------------------------------


# Hosts that count as "public" — repos pointing at them don't need an
# internal-registry passthrough. Kept narrow on purpose: anything not
# on this list is treated as private and warned about.
_PUBLIC_REGISTRY_HOSTS = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
    }
)


def _passthrough_appears_wired(home: Path, extra_mounts_env: str) -> bool:
    """True iff the worker is configured to forward an internal-registry
    config file into the container.

    Either:
      * one of the AUTO_MOUNT_REGISTRY_PATHS exists on the host, OR
      * ORCH_WORKER_EXTRA_MOUNTS names at least one existing path.
    """
    for host_path, _container in _auto_mounted_registry_paths(home):
        if host_path.is_file():
            return True
    return bool(_parse_extra_mounts({EXTRA_MOUNTS_ENV: extra_mounts_env}, home))


def _registry_field_from_package_json(repo: Path) -> str | None:
    """Pull the `"registry"` field out of `<repo>/package.json` if any.

    Returns the URL string (truthy) or None (falsy) so callers can use
    it directly in a conditional. Malformed JSON, missing file, missing
    field — all return None without raising.
    """
    pkg = repo / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("registry")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _index_url_from_requirements_txt(repo: Path) -> str | None:
    """Return the first `--index-url <url>` value in requirements.txt, if any.

    Skips the file when missing or unreadable. Public pypi URLs are
    handled by the caller (they don't constitute a passthrough need).
    """
    req = repo / "requirements.txt"
    if not req.is_file():
        return None
    try:
        text = req.read_text()
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Match both `--index-url <url>` and `--index-url=<url>`.
        for prefix in ("--index-url=", "-i "):
            if line.startswith(prefix):
                return line[len(prefix) :].strip().split()[0]
        if line.startswith("--index-url "):
            return line.split(None, 1)[1].strip().split()[0]
    return None


def _is_private_registry_url(url: str) -> bool:
    """True iff `url`'s host is NOT in the small public-registry allowlist.

    A bare hostname (no scheme) is treated as private — internal artifactory
    URLs often look like `artifactory.internal/path` without https://.
    """
    # Extract the host between the scheme and the first /. urlparse would
    # work too, but a lightweight regex keeps stdlib usage shallow.
    match = re.match(r"^[a-zA-Z][\w+.\-]*://([^/]+)", url)
    if match:
        netloc = match.group(1)
    else:
        netloc = url.split("/", 1)[0]
    host = netloc.split("@")[-1].split(":")[0].lower()
    return host not in _PUBLIC_REGISTRY_HOSTS


def audit_registry_passthrough_for_repo(
    repo: Path,
    *,
    home_dir: Path | None = None,
    extra_mounts_env: str | None = None,
) -> list[str]:
    """Return warnings if a repo needs internal-registry access but none is wired.

    Heuristics — both run independently so a repo using BOTH npm and pip
    against internal hosts surfaces both warnings:

      * `package.json` carries a `"registry"` field pointing at a non-
        public host.
      * `requirements.txt` carries a `--index-url <url>` pointing at a
        non-public host.

    Returns an empty list when no passthrough is needed OR when a
    passthrough already appears to be wired (`_passthrough_appears_wired`).

    End-to-end user experience (PR #14 review C5) — what happens when a
    repo needs internal-registry access:

      1. **Detection.** This function reads `package.json` and
         `requirements.txt` from the cloned repo. Either a `registry`
         field or a `--index-url` pointing at a non-allowlisted host
         (not in `_PUBLIC_REGISTRY_HOSTS`) is treated as "needs
         passthrough".

      2. **Wiring check.** `_passthrough_appears_wired(home, env)`
         returns True if any of `AUTO_MOUNT_REGISTRY_PATHS` exists on
         the host OR `ORCH_WORKER_EXTRA_MOUNTS` names an existing path.
         When True, this function returns `[]` — no warning fires.

      3. **Green path (passthrough wired).** No output here. At spawn
         time the cred-audit (`build_cred_audit().render()`) enumerates
         every auto-mounted file under "Mounts into worker" so the user
         sees what landed inside the container.

      4. **Yellow path (passthrough not wired).** Returns one warning
         per detected need, each naming the exact file to populate:
         "Place ~/.npmrc on the host or set ORCH_WORKER_EXTRA_MOUNTS=
         <path-to-npmrc>." The doctor command surfaces these warnings
         before the user runs into the failure mid-spawn.

      5. **Red path (passthrough wired but install still fails inside
         the container).** Two likely causes:

           a. The bind-mounted credentials are stale / lack permissions
              for the requested package. Inspect `~/.npmrc` etc. on the
              host directly — the container sees a verbatim copy.

           b. The registry's hostname isn't on the dnsmasq DNS allowlist
              (orchestrator/network/allowlist.dnsmasq.conf), so DNS
              resolves to 0.0.0.0 and the connect fails. Mitigation:
              add the host to `ORCH_INTERNAL_REGISTRY_HOSTS` (comma-
              separated). The cred-audit surfaces every host added this
              way under "Internal registry hosts (added to DNS
              allowlist)" so you can verify the env var landed.

         Worker stdout/stderr from the failed `npm install` / `pip
         install` ends up in the unit's event timeline
         (`unit_history(unit_id)`) — that's the single place to read
         the install error message.
    """
    home = home_dir if home_dir is not None else Path.home()
    extras = (
        extra_mounts_env if extra_mounts_env is not None else os.environ.get(EXTRA_MOUNTS_ENV, "")
    )

    npm_registry = _registry_field_from_package_json(repo)
    pip_index = _index_url_from_requirements_txt(repo)

    needs_npm = bool(npm_registry and _is_private_registry_url(npm_registry))
    needs_pip = bool(pip_index and _is_private_registry_url(pip_index))

    if not (needs_npm or needs_pip):
        return []
    if _passthrough_appears_wired(home, extras):
        return []

    warnings: list[str] = []
    if needs_npm:
        warnings.append(
            f"package.json declares registry={npm_registry!r} but no internal-"
            "registry passthrough is wired. Place ~/.npmrc on the host or set "
            "ORCH_WORKER_EXTRA_MOUNTS=<path-to-npmrc>."
        )
    if needs_pip:
        warnings.append(
            f"requirements.txt has --index-url={pip_index!r} but no internal-"
            "registry passthrough is wired. Place ~/.pip/pip.conf on the host "
            "or set ORCH_WORKER_EXTRA_MOUNTS=<path-to-pip.conf>."
        )
    return warnings


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

    The 4th probe (orch-net network exists) plugs a pre-flight gap:
    without it `audit.render()` would print "Network: orch-net" like a
    fact but the actual spawn would error with
    "network orch-net not found".
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

    # 4) orch-net bridge network exists. A missing network is the
    # most-common spawn-time failure mode for users who skipped
    # `scripts/run-worker-dns.sh`, and the audit's "Network: orch-net"
    # line reads like a guarantee. Probe surfaces the gap pre-flight
    # with a fix-it hint.
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
    # Optional per-instance override of the spawn/resume subprocess
    # timeout in seconds. None means: fall back to ORCH_WORKER_TIMEOUT_SECONDS
    # from the env, then to DEFAULT_SPAWN_TIMEOUT_SECONDS. Tests use this
    # to drive a 10s timeout against a deliberately hanging in-container
    # command (F-001-U-6).
    timeout_seconds: int | None = None

    def _runner(self) -> SubprocessRunner:
        return self.run if self.run is not None else subprocess.run

    def _emit(self, msg: str) -> None:
        if self.log is None:
            return
        # Logging must never crash a spawn; suppress all logger failures.
        import contextlib

        with contextlib.suppress(Exception):
            self.log(msg)

    def _resolve_timeout(self, host_env: dict[str, str] | None = None) -> int:
        """Pick the effective subprocess timeout (see `_resolve_timeout_seconds`)."""
        return _resolve_timeout_seconds(self.timeout_seconds, host_env)

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

        # ----- F-001-U-4: internal-registry passthrough (default-on) -----
        # Auto-mount registry config files when present on the host. The
        # cred-audit lists every path so the user sees what crossed.
        for host_path, container_path in _auto_mounted_registry_paths(self.home_dir):
            argv += [
                "--mount",
                f"type=bind,source={host_path},target={container_path},readonly",
            ]
        # Opt-in extra mounts via ORCH_WORKER_EXTRA_MOUNTS in the host
        # env. Filtered to paths that actually exist; missing entries
        # are silently dropped (docker would error otherwise).
        env_for_extras: Mapping[str, str] = host_env if host_env is not None else dict(os.environ)
        for extra in _parse_extra_mounts(env_for_extras, self.home_dir):
            target = _container_target_for_extra(extra, self.home_dir)
            argv += [
                "--mount",
                f"type=bind,source={extra},target={target},readonly",
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
        timeout = self._resolve_timeout()
        try:
            proc = runner(
                argv,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"docker run timed out after {timeout}s for {self.role} spawn "
                f"(ORCH_WORKER_TIMEOUT_SECONDS or DockerClaudeCodeWorker."
                f"timeout_seconds controls this budget)"
            ) from exc
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
        timeout = self._resolve_timeout()
        try:
            proc = runner(
                argv,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"docker run timed out after {timeout}s for {self.role} resume "
                f"(ORCH_WORKER_TIMEOUT_SECONDS or DockerClaudeCodeWorker."
                f"timeout_seconds controls this budget)"
            ) from exc
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

    def tail_messages(self, session_id: str, *, limit: int = 50) -> TailResult:
        """Return the worker container's state + its most recent messages.

        Two reads, both safe to call mid-cycle:
          1. ``docker inspect <session_id>`` for container state. The
             container is expected to be named after the session id
             (the contract F-008's spawn-side wiring follows).
          2. The Claude Code session JSONL at
             ``<home>/.claude/sessions/<session_id>.jsonl`` for the
             recent agent messages. The bind-mounted sessions dir is
             persisted across container lifetimes so we can read it
             even after ``--rm`` removed the container.

        Status resolution (see ``_DOCKER_STATE_MAP`` for the full table):
          * container State.Status == 'running'            → ``running``
          * State.Status == 'exited' & ExitCode == 0       → ``idle``
          * State.Status == 'exited' & ExitCode != 0       → ``terminated``
            (``reason='exit_code=<code>'``)
          * State.Status == 'restarting'                   → ``running``
          * State.Status ∈ {created,paused,dead,removing}  → ``terminated``
            (``reason='container_state=<state>'``)
          * any other (future) State.Status                → ``terminated``
            (fail-safe — caller shouldn't wait on an unknown state)
          * no container, session JSONL exists             → ``idle``
            (``--rm`` removed the finished container)
          * no container, no session JSONL                 → ``not_found``
          * ``docker inspect`` failed (daemon down,
            permission denied, timeout, …)                 → ``terminated``
            with ``reason='docker inspect failed: <detail>'`` — the user
            sees the operational outage rather than a misleading
            ``not_found``.

        Note (B1 from PR #29 review): the ``running`` branch is not yet
        reachable in production runs. ``spawn()`` does not pass
        ``--name <session_id>`` to ``docker run`` today, so for a real
        F-008 cycle ``docker inspect`` will return "No such object" and
        this method falls through to ``idle`` (when the JSONL exists) or
        ``not_found``. Wiring the ``--name`` flag into ``spawn()`` is a
        follow-up unit; this unit ships the abstraction + the JSONL +
        inspect plumbing the follow-up will rely on.
        """
        _validate_limit(limit)
        inspect = self._inspect_container(session_id)
        messages = _read_session_messages(self.home_dir, session_id, limit)

        if not inspect["found"]:
            err = inspect.get("error")
            if err:
                # Operational outage (daemon down, permission denied,
                # timeout). Caller shouldn't wait on the session AND
                # needs the underlying cause visible — `terminated` +
                # `reason=` covers both.
                return {
                    "status": "terminated",
                    "messages": messages,
                    "reason": f"docker inspect failed: {err}",
                }
            # Legitimate "no such object" — either the worker finished
            # and `--rm` removed it (JSONL surfaces the work that ran)
            # or the session is genuinely unknown.
            if _session_file_exists(self.home_dir, session_id):
                return {"status": "idle", "messages": messages, "reason": None}
            return {"status": "not_found", "messages": [], "reason": None}

        state = inspect["status"]
        exit_code = inspect["exit_code"]
        if state == "running":
            return {"status": "running", "messages": messages, "reason": None}
        if state == "exited":
            if exit_code == 0:
                return {"status": "idle", "messages": messages, "reason": None}
            return {
                "status": "terminated",
                "messages": messages,
                "reason": f"exit_code={exit_code}",
            }
        # Non-running, non-exited container states. Map per
        # `_DOCKER_STATE_MAP`; default to `terminated` for future states
        # so the unknown case fails toward "don't wait on it" (per
        # PR #29 review C2).
        mapped = _DOCKER_STATE_MAP.get(state, "terminated")
        return {
            "status": mapped,
            "messages": messages,
            "reason": f"container_state={state}",
        }

    def _inspect_container(self, name: str) -> dict[str, Any]:
        """Run ``docker inspect`` and return a discriminated result dict.

        Return shapes:
          * ``{"found": True, "status": <state>, "exit_code": <int>}``
            — container exists; caller maps ``status`` per the state
            table.
          * ``{"found": False}`` — ``No such object`` / ``No such
            container``. The normal post-``--rm`` state; ``tail_messages``
            falls back to the JSONL.
          * ``{"found": False, "error": "<detail>"}`` — anything else
            (daemon down, permission denied, timeout, non-zero exit
            with unrecognized stderr, empty stdout). The caller surfaces
            the detail in ``TailResult.reason`` instead of pretending
            it was a missing container.

        Exception handling is narrowed to the subprocess module's own
        errors plus ``OSError`` / ``FileNotFoundError`` (the latter
        happens when ``docker`` itself isn't on ``$PATH``). Programmer
        errors (a bad argv from a future edit, etc.) propagate so they
        surface in tests rather than masquerading as a missing
        container.
        """
        runner = self._runner()
        try:
            proc = runner(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}} {{.State.ExitCode}}",
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
            # Subprocess-level failure: docker CLI missing, daemon
            # connection refused at the OS level, 10s timeout. Caller
            # gets `terminated` + `reason=docker inspect failed: ...`.
            return {"found": False, "error": f"{type(exc).__name__}: {exc}"}

        returncode = getattr(proc, "returncode", 1)
        stderr = (getattr(proc, "stderr", "") or "").strip()

        if returncode != 0:
            # `docker inspect` prints "Error: No such object: <name>" for
            # a missing container. Anything else with non-zero exit is
            # an operational outage we should surface — daemon down,
            # permission denied on the socket, etc.
            if "No such object" in stderr or "No such container" in stderr:
                return {"found": False}
            return {"found": False, "error": stderr or f"exit code {returncode}"}

        out = (getattr(proc, "stdout", "") or "").strip()
        if not out:
            return {"found": False, "error": "empty inspect output"}

        parts = out.split()
        status = parts[0]
        exit_code = 0
        if len(parts) > 1:
            try:
                exit_code = int(parts[1])
            except ValueError:
                exit_code = 0
        return {"found": True, "status": status, "exit_code": exit_code}


def _session_jsonl_path(home: Path, session_id: str) -> Path:
    """Canonical location of a Claude Code session's JSONL transcript."""
    return home / ".claude" / "sessions" / f"{session_id}.jsonl"


def _session_file_exists(home: Path, session_id: str) -> bool:
    return _session_jsonl_path(home, session_id).is_file()


# Docker container ``State.Status`` → ``TailStatus`` for the non-running,
# non-exited cases. ``running`` and ``exited`` are special-cased in
# ``tail_messages`` (the exited branch needs the exit code) so they're
# not in this table. Unknown states default to ``terminated`` at the
# call site so a future docker state doesn't get misreported as
# in-flight — see PR #29 review C2.
_DOCKER_STATE_MAP: dict[str, TailStatus] = {
    "restarting": "running",  # transient; expected to return to running
    "paused": "terminated",  # suspended; caller shouldn't wait for output
    "created": "terminated",  # exists but never started — no work to tail
    "dead": "terminated",  # daemon couldn't deliver signal — won't recover
    "removing": "terminated",  # mid-teardown — no further messages coming
}


# Outer-record ``type`` values that count as assistant output in claude's
# JSONL. Anything else (``user``, ``tool_use``, ``tool_result``, ``system``,
# …) is filtered out by ``_extract_tail_message`` so the docker backend's
# tail matches the managed-agent backend's ``types=['agent.message']``
# filter and doesn't leak user prompts into the result (PR #29 review C1).
_ASSISTANT_OUTER_TYPES = frozenset({"assistant", "agent", "agent.message"})
# Inner ``message.role`` values that count as assistant output.
_ASSISTANT_ROLES = frozenset({"assistant", "agent"})


def _read_session_messages(home: Path, session_id: str, limit: int) -> list[TailMessage]:
    """Parse the session JSONL into ``TailMessage`` dicts, capped at ``limit``.

    Permissive on shape, strict on speaker. The two layouts that show
    up across Claude Code CLI versions:

      * ``{type, role, text, ts}`` flat on the object
      * ``{type, timestamp, message: {role, content: <str | list[block]>}}``
        — the current Claude Code shape, where ``content`` may be a
        plain string or a list of typed blocks each carrying ``text``.

    Either way, the record is kept ONLY when the outer ``type`` or
    inner ``message.role`` identifies the speaker as the assistant.
    User prompts, tool-use turns, tool-result turns, and system
    records are dropped (PR #29 review C1).

    Lines that don't parse, lines that aren't a recognized shape, and
    lines without any text payload are silently skipped. Returns the
    most recent ``limit`` entries in chronological order.
    """
    path = _session_jsonl_path(home, session_id)
    if not path.is_file():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    out: list[TailMessage] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        msg = _extract_tail_message(obj)
        if msg is not None:
            out.append(msg)
    return out[-limit:]


def _extract_tail_message(obj: object) -> TailMessage | None:
    """Map one JSONL record onto the ``TailMessage`` shape, or ``None``.

    Returns ``None`` for any record whose speaker is NOT the assistant
    (per ``_ASSISTANT_OUTER_TYPES`` / ``_ASSISTANT_ROLES``). See
    ``_read_session_messages`` for the supported record layouts.
    """
    if not isinstance(obj, dict):
        return None
    outer_type = obj.get("type")
    ts = str(obj.get("timestamp") or obj.get("processed_at") or obj.get("created_at") or "")

    # Flat shape: {role, text, ...}
    flat_role = obj.get("role")
    flat_text = obj.get("text")
    if (
        isinstance(flat_role, str)
        and flat_role in _ASSISTANT_ROLES
        and isinstance(flat_text, str)
        and flat_text
    ):
        return {"ts": ts, "role": flat_role, "text": flat_text}

    # Nested shape: {type, message: {role, content}}. Require the outer
    # type to identify assistant output — the nested role check is a
    # second line of defense for older shapes where `type` is absent.
    nested = obj.get("message")
    if not isinstance(nested, dict):
        return None
    role = nested.get("role")
    if not isinstance(role, str) or role not in _ASSISTANT_ROLES:
        return None
    if outer_type is not None and outer_type not in _ASSISTANT_OUTER_TYPES:
        return None
    text = _coerce_content_text(nested.get("content"))
    if not text:
        return None
    return {"ts": ts, "role": role, "text": text}


def _coerce_content_text(content: object) -> str:
    """Reduce claude's ``content`` value to a single text string.

    The field is either a string (older schemas) or a list of block
    dicts (current), each block typically ``{type: 'text', text: '...'}``
    but tolerated to carry the raw string directly.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


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
    "AUTO_MOUNT_REGISTRY_PATHS",
    "DEFAULT_CPUS",
    "DEFAULT_DNS",
    "DEFAULT_DNS_SEARCH",
    "DEFAULT_IMAGE",
    "DEFAULT_MEMORY",
    "DEFAULT_NETWORK",
    "DEFAULT_PIDS_LIMIT",
    "DEFAULT_SPAWN_TIMEOUT_SECONDS",
    "EXTRA_MOUNTS_ENV",
    "INTERNAL_REGISTRY_HOSTS_ENV",
    "NEVER_MOUNTED_HOST_PATHS",
    "SENSITIVE_ENV_NAMES",
    "SENSITIVE_ENV_PREFIXES",
    "TIMEOUT_ENV",
    "AuthMode",
    "CredAudit",
    "DockerClaudeCodeWorker",
    "DoctorProbeResult",
    "audit_registry_passthrough_for_repo",
    "build_cred_audit",
    "extract_session_id",
    "run_doctor_probes",
    "select_auth_mode",
]
