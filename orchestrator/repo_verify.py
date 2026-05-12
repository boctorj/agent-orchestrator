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
        if auth_mode == "pat":
            ur = client.get(f"{GITHUB_API_BASE}/user")
            if ur.status_code == 200:
                token_user_login = ur.json().get("login", "") or ""
                result.auth_identity = f"user:{token_user_login or '?'}"
        elif auth_mode == "app":
            inst = os.getenv("GITHUB_APP_INSTALLATION_ID", "?")
            result.auth_identity = f"app:installation:{inst}"

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
    return lines


__all__ = [
    "GITHUB_API_BASE",
    "detect_stale_env",
    "format_result_lines",
    "normalize_repo_url",
    "verify",
]
