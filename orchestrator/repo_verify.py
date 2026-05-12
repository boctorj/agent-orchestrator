"""GitHub-side verification of a target repo against the orchestrator's policy.

A repo is verifiable only if all of the following hold:

  1. The configured token can READ the repo (`permissions.pull = true`).
  2. The configured token can WRITE to the repo (`permissions.push = true`).
  3. Branch protection rules exist on the repo's default branch.
  4. Required-approving-review count is at least 1.
  5. Force pushes are blocked (`allow_force_pushes = false`).
  6. Branch deletion is blocked (`allow_deletions = false`).
  7. Admins cannot bypass protection (`enforce_admins = true`).
  8. (App auth only) The repo is in the App installation's repo list.

Non-blocking warnings: CODEOWNERS present (may block bot endorsement),
required signed commits (bot commits aren't signed), required status
checks (CI must pass — informational).

Pre-flight probes (non-blocking, F-005): a focused subset of the
BLOCKED-reason taxonomy is also probed here so the lead catches them
at planning time instead of after a coder/tester burns a session:

  - `branch_protection_blocked_push` — feature-branch rulesets that
    would 403 the orchestrator's direct push of a coder branch.
  - `auth_failure` — token scope insufficient for spawn-time API calls
    (detectable subset only — full validation needs a live push).
  - `network_error` — transport-layer failure reaching api.github.com.

Each probe emits a WARN row tagged with its reason slug; the report
appends a hint table mapping slug -> fix-it guidance. WARN does NOT
block — spawns continue to be gated only on the pass/fail checks.

The orchestrator caches a passing VerificationResult in
state.verified_repos for VERIFY_TTL_HOURS hours. Inside that window,
spawns trust the cache. After the TTL, the orchestrator re-verifies
transparently on next access.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from orchestrator.models import CheckResult, VerificationResult

GITHUB_API_BASE = "https://api.github.com"

# Regex matches the standard KEY=value form in a .env file (one per line).
# The same shape `orchestrator doctor` uses — kept consistent so the two
# call sites can't drift on what "the GITHUB_TOKEN in .env" means.
_ENV_GITHUB_TOKEN_RE = re.compile(r"^GITHUB_TOKEN=(\S+)", re.MULTILINE)


# --------------------------- Pre-flight reason slugs ---------------------------
#
# These slugs are the planning-time-catchable subset of the BLOCKED-reason
# taxonomy used by escalation summaries and ntfy bodies (feature F-005).
# Pinned here as constants so verify_repo, escalation summary rendering, and
# the dashboard can all reference the same string without typo drift.
#
# `disk_full` / `rate_limited` and other non-pre-flightable reasons live in
# the taxonomy but are deliberately NOT probed here — verify_repo can't see
# the future runtime environment of a coder agent.

REASON_BRANCH_PROTECTION_BLOCKED_PUSH = "branch_protection_blocked_push"
REASON_AUTH_FAILURE = "auth_failure"
REASON_NETWORK_ERROR = "network_error"

PREFLIGHT_REASON_SLUGS: frozenset[str] = frozenset(
    {
        REASON_BRANCH_PROTECTION_BLOCKED_PUSH,
        REASON_AUTH_FAILURE,
        REASON_NETWORK_ERROR,
    }
)

# Fix-it hints keyed by slug. Rendered as a small table at the bottom of
# the verify_repo report whenever a tagged warning fires. Other surfaces
# (escalation summaries, ntfy push bodies) are expected to import this
# dict so the same hint renders consistently wherever a slug appears.
PREFLIGHT_HINTS: dict[str, str] = {
    REASON_BRANCH_PROTECTION_BLOCKED_PUSH: (
        "scope branch protection to `main` only, add the orchestrator "
        "identity as a bypass actor on the feature-branch ruleset, or "
        "issue a bypass-capable PAT for the spawn surface"
    ),
    REASON_AUTH_FAILURE: (
        "re-issue the PAT with the `repo` scope (or `public_repo` for "
        "public-only repos); for App auth, grant Contents: write and "
        "Pull requests: write on the installation"
    ),
    REASON_NETWORK_ERROR: (
        "check connectivity to api.github.com (firewall / proxy / DNS); "
        "transient failures may resolve on retry, persistent ones will "
        "block every spawn"
    ),
}

# Synthetic feature-branch name used by the feature-branch push probe.
# Deliberately recognizable so anyone inspecting GitHub audit logs sees
# what it is. The probe NEVER creates or pushes this branch — it only
# asks GitHub's Rules API which rules WOULD apply if it did.
_PROBE_BRANCH_NAME = "agent-orchestrator-preflight"

# Slug-tag prefix conventions for the warnings list. Kept as constants so
# `extract_warning_slug` and `_tag_warning` stay in sync.
_WARN_TAG_OPEN = "["
_WARN_TAG_CLOSE = "] "


def _tag_warning(slug: str, message: str) -> str:
    """Format a warning string with the reason-slug prefix.

    The prefix lets the hint-table renderer (and any escalation surface
    that reads warnings) parse a structured reason out of an otherwise
    free-form line.
    """
    return f"{_WARN_TAG_OPEN}{slug}{_WARN_TAG_CLOSE}{message}"


def extract_warning_slug(warning: str) -> str | None:
    """Return the slug embedded in a tagged warning string, or None.

    Looks for the leading `[slug] ` prefix produced by `_tag_warning`.
    Returns the slug only if it's in `PREFLIGHT_REASON_SLUGS` — bare
    bracketed text in legacy warnings does not get classified as a slug.
    """
    if not warning.startswith(_WARN_TAG_OPEN):
        return None
    end = warning.find(_WARN_TAG_CLOSE)
    if end <= len(_WARN_TAG_OPEN):
        return None
    slug = warning[len(_WARN_TAG_OPEN) : end]
    if slug in PREFLIGHT_REASON_SLUGS:
        return slug
    return None


# --------------------------- URL canonicalization ---------------------------


def normalize_repo_url(url: str) -> str:
    """Return a canonical https://github.com/owner/repo URL.

    Accepts:
      - https://github.com/owner/repo
      - github.com/owner/repo
      - https://github.com/owner/repo/  (trailing slash stripped)
      - https://github.com/owner/repo.git (`.git` suffix stripped)
      - https://github.com/Owner/Repo  (lowercased)

    Rejects (raises ValueError):
      - Non-github.com hosts (GitHub Enterprise not yet supported)
      - SSH form `git@github.com:owner/repo` (the orchestrator authenticates
        via HTTPS tokens; SSH auth is out of scope)
      - Paths that aren't exactly /owner/repo
    """
    s = url.strip()
    if not s:
        raise ValueError("empty repo URL")
    if s.startswith("git@"):
        raise ValueError(
            "SSH-form URLs (git@github.com:owner/repo) aren't supported — "
            "use https://github.com/owner/repo"
        )
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    parsed = urlparse(s)
    if parsed.netloc.lower() != "github.com":
        raise ValueError(
            f"only github.com is supported (got {parsed.netloc!r}); "
            "GitHub Enterprise tracked in BACKLOG.md"
        )
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"expected /owner/repo, got {parsed.path!r}")
    owner, repo = parts
    return f"https://github.com/{owner.lower()}/{repo.lower()}"


def _owner_repo(repo_url: str) -> tuple[str, str]:
    """Pull (owner, repo) out of an already-normalized URL."""
    path = repo_url.removeprefix("https://github.com/")
    owner, repo = path.split("/", 1)
    return owner, repo


# --------------------------- HTTP helpers ---------------------------


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-orchestrator",
    }


# --------------------------- identity match ---------------------------


def _identity_match_check(
    client: httpx.Client, owner: str, repo: str, token_user: str
) -> CheckResult:
    """Build the "identity match" CheckResult for a PAT-authenticated verify().

    Pass cases:
      * token user IS the repo owner (case-insensitive login compare), OR
      * token user appears in the collaborators list with push permission.

    Failure case (the bug F-002 exists to fix): token user is neither the
    owner nor a push-capable collaborator. The detail string includes the
    exact fix-it instructions a user needs — generate a PAT from the owner
    account, or grant the current user push access on this repo.
    """
    if token_user.lower() == owner.lower():
        return CheckResult(
            "identity match",
            True,
            f"token user ({token_user}) is the repo owner",
        )

    # Different login. Check collaborators with push.
    cr = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/collaborators")
    push_capable_collaborators: set[str] = set()
    if cr.status_code == 200:
        for collab in cr.json() or []:
            login = (collab.get("login") or "").lower()
            perms = collab.get("permissions") or {}
            if login and perms.get("push"):
                push_capable_collaborators.add(login)

    if token_user.lower() in push_capable_collaborators:
        return CheckResult(
            "identity match",
            True,
            f"token user ({token_user}) is a collaborator with push access",
        )

    detail = (
        f"token user ({token_user}) is not the repo owner ({owner}) "
        f"and is not a collaborator with push access. Generate a PAT "
        f"from the {owner} account, or grant {token_user} push access "
        f"on this repo."
    )
    return CheckResult("identity match", False, detail)


# --------------------------- stale-env detection ---------------------------


def detect_stale_env(env_path: Path | str = Path(".env")) -> str | None:
    """Return a warning if the loaded GITHUB_TOKEN differs from the .env file's.

    The MCP server reads `.env` once at startup via `load_dotenv()`. If the
    user has since edited `.env` (e.g., rotated the token) but hasn't
    restarted the server, the in-process `os.environ['GITHUB_TOKEN']`
    still holds the OLD value. That's invisible to the user and surfaces
    only as confusing downstream auth failures.

    This helper compares the two and returns a multi-line warning string
    when they differ. The token VALUE is never logged — only the fact of
    a mismatch.

    Returns ``None`` when:
      * `.env` doesn't exist on disk (nothing to compare against),
      * `.env` has no `GITHUB_TOKEN` line, or
      * the on-disk value matches the loaded value.
    """
    path = Path(env_path)
    if not path.exists():
        return None
    try:
        content = path.read_text()
    except OSError:
        return None
    match = _ENV_GITHUB_TOKEN_RE.search(content)
    if not match:
        return None
    on_disk = match.group(1)
    loaded = os.environ.get("GITHUB_TOKEN", "")
    if on_disk == loaded:
        return None
    return (
        "⚠ Loaded GITHUB_TOKEN differs from the value currently in .env.\n"
        "  The MCP server cached the old value at startup. Restart the server\n"
        "  to pick up the new token before retrying."
    )


# --------------------------- verify() ---------------------------


def verify(repo_url: str, token: str, auth_mode: str = "pat") -> VerificationResult:
    """Run the full policy verification against ``repo_url`` using ``token``.

    Args:
        repo_url: any of the accepted forms (normalized inside).
        token: GitHub PAT or App installation token with read+write+PR perms.
        auth_mode: ``'pat'`` or ``'app'``. Drives identity-string format and
            whether to additionally check ``/installation/repositories``.

    Returns:
        VerificationResult with one CheckResult per policy item and a
        warnings list. ``result.passed`` is the overall verdict.

    Never raises for normal failure modes (404, missing protection, etc.);
    those land as failing CheckResults. Only raises on programmer error
    (bad URL form, missing token).
    """
    if not token:
        raise ValueError("verify() requires a non-empty token")

    repo_url = normalize_repo_url(repo_url)
    owner, repo = _owner_repo(repo_url)
    result = VerificationResult(repo_url=repo_url, auth_mode=auth_mode)

    with httpx.Client(timeout=10.0, headers=_api_headers(token)) as client:
        # 1. Read access + default branch + permissions
        r = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}")
        if r.status_code == 404:
            result.checks.append(
                CheckResult(
                    "read access",
                    False,
                    "404 — repo not found, or token cannot see it (check scope/installation)",
                )
            )
            return result
        if r.status_code != 200:
            result.checks.append(
                CheckResult(
                    "read access", False, f"HTTP {r.status_code} from /repos/{owner}/{repo}"
                )
            )
            return result

        meta = r.json()
        result.default_branch = meta.get("default_branch", "main")
        perms = meta.get("permissions") or {}
        result.checks.append(CheckResult("read access", bool(perms.get("pull", False))))

        # Auth identity (best-effort — never blocks). Fetched BEFORE the
        # write/branch-protection checks so the identity-match check below
        # can use it; otherwise an identity mismatch surfaces only as a
        # confusing downstream "write access" failure.
        token_user_login = ""  # nosec B105 — placeholder for the PAT branch below, not a secret
        user_resp = None  # captured for the scope probe below
        if auth_mode == "pat":
            ur = client.get(f"{GITHUB_API_BASE}/user")
            if ur.status_code == 200:
                token_user_login = ur.json().get("login", "") or ""
                result.auth_identity = f"user:{token_user_login or '?'}"
            user_resp = ur
        elif auth_mode == "app":
            inst = os.getenv("GITHUB_APP_INSTALLATION_ID", "?")
            result.auth_identity = f"app:installation:{inst}"

        # Pre-flight probe: PAT scope sufficiency (auth_failure).
        # Run as soon as we have a /user response, regardless of whether the
        # rest of verification continues — even on a failure path the WARN
        # is useful context for the lead.
        _probe_token_scopes(user_resp, result, auth_mode)

        # 1.5. Identity match — does the token user own this repo, or are
        # they at least a push-capable collaborator? Runs BEFORE write/
        # branch-protection so the root cause surfaces as a first-class
        # check instead of as a downstream symptom. PAT-only: in App mode
        # the identity is `app:installation:N`, which doesn't compare
        # 1:1 with a repo owner login.
        if auth_mode == "pat" and token_user_login:
            result.checks.append(_identity_match_check(client, owner, repo, token_user_login))

        # Write access check (uses cached perms from the /repos call above).
        result.checks.append(
            CheckResult(
                "write access",
                bool(perms.get("push", False)),
                "" if perms.get("push") else "token lacks push permission for this repo",
            )
        )

        # Pre-flight probe: default-branch reachability (network_error).
        # Touches /branches/<default> separately from the protection call so
        # a transport-layer failure is classified with the slug instead of
        # bubbling up as an opaque exception. HTTP errors (4xx/5xx) here
        # are NOT classified as network_error — those are surfaced through
        # the pass/fail checks already (read access, branch protection).
        _probe_default_branch_reachable(client, result, owner, repo, result.default_branch)

        # 2. Branch protection on the default branch
        br = client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{result.default_branch}/protection"
        )
        if br.status_code == 404:
            result.checks.append(
                CheckResult(
                    "branch protection exists",
                    False,
                    f"no branch protection rule on `{result.default_branch}` — "
                    f"set one up at github.com/{owner}/{repo}/settings/branches",
                )
            )
            return result  # short-circuit; sub-rules are meaningless without protection
        if br.status_code != 200:
            result.checks.append(
                CheckResult(
                    "branch protection exists",
                    False,
                    f"HTTP {br.status_code} reading protection rule "
                    f"(token may lack admin:repo_hook or admin:org scope)",
                )
            )
            return result

        result.checks.append(CheckResult("branch protection exists", True))
        prot = br.json()

        # 3. ≥1 approving review required
        rev = prot.get("required_pull_request_reviews") or {}
        n_approvals = int(rev.get("required_approving_review_count") or 0)
        result.checks.append(
            CheckResult(
                "≥1 approving review required",
                n_approvals >= 1,
                f"required_approving_review_count = {n_approvals}",
            )
        )

        # 4. No force push
        force_push = bool((prot.get("allow_force_pushes") or {}).get("enabled", True))
        result.checks.append(
            CheckResult(
                "force push blocked",
                not force_push,
                "allow_force_pushes is enabled" if force_push else "",
            )
        )

        # 5. No deletion
        deletion = bool((prot.get("allow_deletions") or {}).get("enabled", True))
        result.checks.append(
            CheckResult(
                "deletion blocked",
                not deletion,
                "allow_deletions is enabled" if deletion else "",
            )
        )

        # 6. No admin bypass
        enforce_admins = bool((prot.get("enforce_admins") or {}).get("enabled", False))
        result.checks.append(
            CheckResult(
                "admin bypass blocked",
                enforce_admins,
                "" if enforce_admins else "enforce_admins is off — admins can bypass protection",
            )
        )

        # Pre-flight probe: feature-branch push permissions
        # (branch_protection_blocked_push). Asks GitHub's Rules API which
        # rules would apply to a hypothetical feature-branch name; warns
        # if a `pull_request` rule would reject a direct push.
        _probe_feature_branch_push(client, result, owner, repo)

        # 7. App-only: this repo is in the App installation
        if auth_mode == "app":
            inst_r = client.get(f"{GITHUB_API_BASE}/installation/repositories?per_page=100")
            if inst_r.status_code == 200:
                inst_repos = inst_r.json().get("repositories", [])
                full_names = {r["full_name"].lower() for r in inst_repos}
                expected = f"{owner}/{repo}"
                result.checks.append(
                    CheckResult(
                        "App installation covers this repo",
                        expected in full_names,
                        ""
                        if expected in full_names
                        else f"{expected} not in installation's repo list "
                        "(install the App on this repo)",
                    )
                )
            else:
                result.checks.append(
                    CheckResult(
                        "App installation covers this repo",
                        False,
                        f"HTTP {inst_r.status_code} from /installation/repositories",
                    )
                )

        # --- Notes (positive informational) ---

        # CODEOWNERS at the standard paths — this is a POSITIVE signal. With
        # CODEOWNERS in place, GitHub auto-requests review from the owning team
        # on every PR the bot opens. That's the safety model we want for
        # production repos: bot opens PR, humans approve, humans merge. The
        # orchestrator's reviewer agent shifts from "approver" to "pre-screener."
        for path in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
            co = client.get(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": result.default_branch},
            )
            if co.status_code == 200:
                result.notes.append(
                    f"CODEOWNERS present at {path} — the owning team will be auto-requested "
                    "as reviewer on bot PRs. This is the recommended production setup: "
                    "humans gate merges, the reviewer agent pre-screens. Expect to see "
                    "REVIEW_RECOMMEND_MERGE outcomes, not REVIEW_APPROVED — that's by design."
                )
                break

        # --- Warnings (non-blocking, things to fix) ---

        # Required signed commits
        if bool((prot.get("required_signatures") or {}).get("enabled", False)):
            result.warnings.append(
                "required_signatures is on — agent commits aren't GPG-signed by default; "
                "PRs will be blocked from merging"
            )

        # Required status checks (informational; CI failing blocks merge)
        rsc = prot.get("required_status_checks") or {}
        if rsc.get("contexts") or rsc.get("checks"):
            n_ctx = len(rsc.get("contexts") or []) + len(rsc.get("checks") or [])
            result.warnings.append(
                f"{n_ctx} required status check(s) configured — CI must pass before merge"
            )

    return result


# --------------------------- Pre-flight probes ---------------------------
#
# Each probe is defensive: any unexpected response / exception falls through
# without warning so a flaky probe never breaks the main verify() flow.
# Probes only ever APPEND to `result.warnings` — they do not add or mutate
# pass/fail checks. WARN is non-blocking by design (see module docstring).


def _probe_default_branch_reachable(
    client: httpx.Client,
    result: VerificationResult,
    owner: str,
    repo: str,
    default_branch: str,
) -> None:
    """Probe: can we reach the default-branch ref via the API?

    Emits a `network_error` warning on transport-layer failures (DNS,
    connection refused, TLS, read timeout). HTTP-level failures are NOT
    classified as network_error — they're surfaced via the pass/fail
    checks (read access, branch protection).
    """
    try:
        client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{default_branch}")
    except httpx.RequestError as e:
        result.warnings.append(
            _tag_warning(
                REASON_NETWORK_ERROR,
                f"could not reach default-branch ref "
                f"({type(e).__name__}: {e}) — spawn-time pushes will hit "
                "the same network issue",
            )
        )


def _probe_token_scopes(
    user_resp: object,
    result: VerificationResult,
    auth_mode: str,
) -> None:
    """Probe: does the configured token have the scopes spawn surfaces need?

    For classic PATs, GitHub returns the granted scopes in the
    `X-OAuth-Scopes` response header. We require `repo` (full) or
    `public_repo` (public-only) to push branches and create PRs.

    Fine-grained PATs return an empty `X-OAuth-Scopes` header; their
    permissions are surfaced through the per-repo `permissions` object
    we already check, so this probe skips silently for fine-grained
    tokens. App tokens don't expose OAuth scopes — also skipped (the
    App installation match check covers them).
    """
    if auth_mode != "pat" or user_resp is None:
        return
    status = getattr(user_resp, "status_code", 0)
    if status != 200:
        return
    headers = getattr(user_resp, "headers", None)
    if headers is None:
        return
    raw = headers.get("X-OAuth-Scopes", "") or ""
    scopes = {s.strip() for s in raw.split(",") if s.strip()}
    if not scopes:
        # Fine-grained PAT or unexpected header shape; skip.
        return
    if "repo" not in scopes and "public_repo" not in scopes:
        scope_list = ", ".join(sorted(scopes)) if scopes else "(none)"
        result.warnings.append(
            _tag_warning(
                REASON_AUTH_FAILURE,
                f"PAT scopes [{scope_list}] do not include `repo` or "
                "`public_repo` — spawn-time pushes and PR creation will "
                "fail with 401/403",
            )
        )


def _probe_feature_branch_push(
    client: httpx.Client,
    result: VerificationResult,
    owner: str,
    repo: str,
) -> None:
    """Probe: would direct push to a coder feature branch be rejected?

    Uses GitHub's branch-rules endpoint
    (`/repos/{owner}/{repo}/rules/branches/{branch}`) with a synthetic
    feature-branch name. The endpoint returns the active rule set as it
    would evaluate for that branch — including rules from rulesets that
    target wildcard patterns like `feat/*` or `**/*`.

    Emits a `branch_protection_blocked_push` warning if a `pull_request`
    rule applies, since the coder's first `git push origin <branch>`
    would be rejected with `Changes must be made through a pull request`.
    Other rule types (non_fast_forward, etc.) are not classified — they
    correspond to behaviors the orchestrator already avoids.

    Silently skips on:
      - non-200 responses (endpoint unsupported, insufficient scope, etc.)
      - transport errors (caught by the reachability probe)
      - unexpected payload shapes
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/rules/branches/{_PROBE_BRANCH_NAME}"
    try:
        r = client.get(url)
    except httpx.RequestError:
        return  # covered by reachability probe
    if getattr(r, "status_code", 0) != 200:
        return
    try:
        payload = r.json()
    except (ValueError, TypeError):
        return
    if not isinstance(payload, list):
        return
    for rule in payload:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") == "pull_request":
            result.warnings.append(
                _tag_warning(
                    REASON_BRANCH_PROTECTION_BLOCKED_PUSH,
                    f"feature-branch ruleset requires pull requests "
                    f"(probed against `{_PROBE_BRANCH_NAME}`) — the "
                    "orchestrator's direct push of a coder branch will "
                    "be rejected with 403 (Changes must be made through "
                    "a pull request)",
                )
            )
            return


# --------------------------- formatting helpers ---------------------------


def format_result_lines(result: VerificationResult) -> list[str]:
    """Return human-readable lines summarizing a VerificationResult.

    Used by the CLI `orchestrator verify-repo` subcommand and by the MCP
    tool's response. Pure formatting; no I/O.
    """
    lines: list[str] = []
    icon = "✓" if result.passed else "✗"
    lines.append(f"{icon} {result.repo_url}")
    if result.default_branch:
        lines.append(f"  default branch: {result.default_branch}")
    if result.auth_identity:
        lines.append(f"  authenticated as: {result.auth_identity} ({result.auth_mode})")
    lines.append("")
    for c in result.checks:
        mark = "✓" if c.passed else "✗"
        line = f"  {mark} {c.name}"
        if c.detail:
            line += f"  · {c.detail}"
        lines.append(line)
    if result.notes:
        lines.append("")
        lines.append("  notes:")
        for n in result.notes:
            lines.append(f"    ℹ {n}")
    if result.warnings:
        lines.append("")
        lines.append("  warnings (non-blocking):")
        for w in result.warnings:
            lines.append(f"    ⚠ {w}")
    # Pre-flight hint table — only render if at least one tagged warning
    # fired. The table maps reason slug -> fix-it hint so the lead sees
    # the same hint copy as escalation summaries and ntfy bodies.
    fired_slugs = [s for s in (extract_warning_slug(w) for w in result.warnings) if s is not None]
    if fired_slugs:
        # Preserve first-seen order, dedupe.
        seen: set[str] = set()
        ordered: list[str] = []
        for s in fired_slugs:
            if s not in seen:
                seen.add(s)
                ordered.append(s)
        lines.append("")
        lines.append("  pre-flight hints (reason -> fix-it):")
        for slug in ordered:
            hint = PREFLIGHT_HINTS.get(slug, "")
            lines.append(f"    • {slug}: {hint}")
    return lines


__all__ = [
    "GITHUB_API_BASE",
    "PREFLIGHT_HINTS",
    "PREFLIGHT_REASON_SLUGS",
    "REASON_AUTH_FAILURE",
    "REASON_BRANCH_PROTECTION_BLOCKED_PUSH",
    "REASON_NETWORK_ERROR",
    "detect_stale_env",
    "extract_warning_slug",
    "format_result_lines",
    "normalize_repo_url",
    "verify",
]
