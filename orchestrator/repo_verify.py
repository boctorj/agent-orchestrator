"""GitHub-side verification of a target repo against the orchestrator's policy.

A repo is verifiable only if all of the following hold:

  1. The configured token can READ the repo (`permissions.pull = true`).
  2. The configured token can WRITE to the repo (`permissions.push = true`).
  3. Branch protection exists on the repo's default branch — via EITHER the
     classic ``/branches/{branch}/protection`` API or the modern Rulesets
     system (``/rules/branches/{branch}``). Either system that satisfies
     orchestrator policy yields green; both is fine too.
  4. Required-approving-review count is at least 1.
  5. Force pushes are blocked (classic: ``allow_force_pushes = false``;
     ruleset: a ``non_fast_forward`` rule applies).
  6. Branch deletion is blocked (classic: ``allow_deletions = false``;
     ruleset: a ``deletion`` rule applies).
  7. Admins cannot bypass protection (classic: ``enforce_admins = true``;
     ruleset: no bypass actor at maintain/admin tier).
  8. (App auth only) The repo is in the App installation's repo list.

When both classic protection and a ruleset apply, classic is preferred for
display (the ``source: classic`` annotation on the "branch protection
exists" check). Ruleset is reported only when classic is absent.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx

from orchestrator.models import CheckResult, VerificationResult

# Symbolic tag identifying which branch-protection system drives the displayed
# sub-checks. Kept narrow (rather than `str`) so the dispatcher's return type
# can't drift from the values verify() and format_result_lines() handle.
ProtectionSource = Literal["classic", "ruleset"]

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


# --------------------------- branch-protection evaluators ---------------------------
#
# verify() consults BOTH the classic /branches/{branch}/protection API and
# the modern Rulesets system (/rules/branches/{branch}). Each evaluator
# returns a uniform `_ProtectionEval` so the dispatcher (`_select_protection_
# source`) can pick which drives the displayed sub-checks. A system is said
# to "pass policy" iff `exists` is True AND every CheckResult in `sub_checks`
# has `passed = True`. The dispatcher prefers the passing system; on a tie it
# prefers classic.

# Bypass-actor tiers that disqualify a ruleset under orchestrator policy.
# The numeric IDs map GitHub's built-in RepositoryRole identifiers:
#   1=read, 2=triage, 3=write, 4=maintain, 5=admin.
# Anything write-tier or below is informational (a write-tier collaborator
# is already trusted to push to feature branches and request review).
# Maintain or admin bypassing protection short-circuits the review gate.
_DISQUALIFYING_REPO_ROLE_IDS: dict[int, str] = {4: "maintain", 5: "admin"}


@dataclass
class _ProtectionEval:
    """Uniform output of a protection-system evaluator (classic or ruleset).

    Attributes:
        exists: True iff the endpoint returned a configured policy. False
            on 404 (no protection) or on a transient HTTP error.
        http_error: Human-readable description when a non-404 HTTP error
            blocked the lookup. Surfaced ONLY when neither evaluator has
            `exists=True`; otherwise the passing system's result is shown.
        sub_checks: The four per-policy CheckResults (approvals, force-push,
            deletion, admin-bypass) translated into the orchestrator's
            common shape so verify() can append them uniformly.
        raw: The original API payload. Only the classic evaluator's `raw`
            is consumed downstream — to extract the classic-only warnings
            (required_signatures, required_status_checks). The ruleset
            evaluator stores its rules list here for future use.
    """

    exists: bool = False
    http_error: str = ""
    sub_checks: list[CheckResult] = field(default_factory=list)
    raw: dict | list = field(default_factory=dict)

    @property
    def passes_policy(self) -> bool:
        """True iff the system exists AND every sub-check passes."""
        return self.exists and all(c.passed for c in self.sub_checks)


def _evaluate_classic_protection(
    client: httpx.Client, owner: str, repo: str, branch: str
) -> _ProtectionEval:
    """Query classic /branches/{branch}/protection; return policy verdict."""
    out = _ProtectionEval()
    br = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches/{branch}/protection")
    if br.status_code == 404:
        return out
    if br.status_code != 200:
        out.http_error = (
            f"HTTP {br.status_code} reading classic protection rule "
            "(token may lack admin:repo_hook or admin:org scope)"
        )
        return out

    out.exists = True
    prot = br.json() or {}
    out.raw = prot

    rev = prot.get("required_pull_request_reviews") or {}
    n_approvals = int(rev.get("required_approving_review_count") or 0)
    out.sub_checks.append(
        CheckResult(
            "≥1 approving review required",
            n_approvals >= 1,
            f"required_approving_review_count = {n_approvals}",
        )
    )
    force_push = bool((prot.get("allow_force_pushes") or {}).get("enabled", True))
    out.sub_checks.append(
        CheckResult(
            "force push blocked",
            not force_push,
            "allow_force_pushes is enabled" if force_push else "",
        )
    )
    deletion = bool((prot.get("allow_deletions") or {}).get("enabled", True))
    out.sub_checks.append(
        CheckResult(
            "deletion blocked",
            not deletion,
            "allow_deletions is enabled" if deletion else "",
        )
    )
    enforce_admins = bool((prot.get("enforce_admins") or {}).get("enabled", False))
    out.sub_checks.append(
        CheckResult(
            "admin bypass blocked",
            enforce_admins,
            "" if enforce_admins else "enforce_admins is off — admins can bypass protection",
        )
    )
    return out


def _evaluate_ruleset_protection(
    client: httpx.Client, owner: str, repo: str, branch: str
) -> _ProtectionEval:
    """Query /rules/branches/{branch} (Rulesets); return policy verdict.

    The endpoint returns a flat list of rules; each rule carries a ``type``,
    ``parameters``, and ``ruleset_id``. To check for disqualifying bypass
    actors we additionally fetch ``/rulesets/{id}`` for each contributing
    ruleset (a best-effort call — non-200 responses are tolerated, since
    the rules endpoint itself already filters out rules the calling token
    can bypass).
    """
    out = _ProtectionEval(raw=[])
    rr = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/rules/branches/{branch}")
    if rr.status_code == 404:
        return out
    if rr.status_code != 200:
        out.http_error = f"HTTP {rr.status_code} from /repos/{owner}/{repo}/rules/branches/{branch}"
        return out

    rules = rr.json() or []
    if not rules:
        # Empty list means no rules apply — same as 404 for our purposes.
        return out

    out.exists = True
    out.raw = rules

    has_pr_rule = False
    max_approvals = 0
    has_non_fast_forward = False
    has_deletion = False
    ruleset_ids: set[int] = set()
    for rule in rules:
        rtype = rule.get("type")
        params = rule.get("parameters") or {}
        if rtype == "pull_request":
            has_pr_rule = True
            n = int(params.get("required_approving_review_count") or 0)
            if n > max_approvals:
                max_approvals = n
        elif rtype == "non_fast_forward":
            has_non_fast_forward = True
        elif rtype == "deletion":
            has_deletion = True
        rsid = rule.get("ruleset_id")
        if rsid is not None:
            ruleset_ids.add(int(rsid))

    # Approvals — same detail format as classic so state.py's regex (which
    # parses "= N" out of detail) keeps working.
    if has_pr_rule:
        out.sub_checks.append(
            CheckResult(
                "≥1 approving review required",
                max_approvals >= 1,
                f"required_approving_review_count = {max_approvals}",
            )
        )
    else:
        out.sub_checks.append(
            CheckResult(
                "≥1 approving review required",
                False,
                "no pull_request rule in ruleset",
            )
        )

    out.sub_checks.append(
        CheckResult(
            "force push blocked",
            has_non_fast_forward,
            "" if has_non_fast_forward else "no non_fast_forward rule in ruleset",
        )
    )
    out.sub_checks.append(
        CheckResult(
            "deletion blocked",
            has_deletion,
            "" if has_deletion else "no deletion rule in ruleset",
        )
    )

    # Inspect each contributing ruleset for disqualifying bypass actors.
    # Sorted for deterministic ordering when multiple rulesets contribute.
    bad_bypass_detail = ""
    for rsid in sorted(ruleset_ids):
        rs = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/rulesets/{rsid}")
        if rs.status_code != 200:
            continue
        rs_json = rs.json() or {}
        for actor in rs_json.get("bypass_actors") or []:
            actor_type = actor.get("actor_type")
            actor_id = actor.get("actor_id")
            if actor_type == "OrganizationAdmin":
                bad_bypass_detail = f"ruleset {rsid} grants OrganizationAdmin a bypass actor entry"
                break
            if actor_type == "RepositoryRole" and actor_id in _DISQUALIFYING_REPO_ROLE_IDS:
                tier = _DISQUALIFYING_REPO_ROLE_IDS[actor_id]
                bad_bypass_detail = (
                    f"ruleset {rsid} grants RepositoryRole {tier}-tier a bypass actor entry"
                )
                break
        if bad_bypass_detail:
            break

    out.sub_checks.append(
        CheckResult(
            "admin bypass blocked",
            not bad_bypass_detail,
            bad_bypass_detail,
        )
    )
    return out


def _select_protection_source(
    classic_eval: _ProtectionEval, ruleset_eval: _ProtectionEval
) -> tuple[ProtectionSource | None, _ProtectionEval | None]:
    """Pick which evaluator drives the displayed sub-checks.

    Returns ``(source, chosen_eval)`` where ``source`` is one of
    ``"classic"``, ``"ruleset"``, or ``None`` (no system has protection).
    The chosen evaluator is returned alongside so callers don't have to
    re-pick it; when ``source is None`` the chosen evaluator is ``None``.

    Selection rules:
      1. If classic passes policy → classic (preferred on a tie).
      2. Else if ruleset passes policy → ruleset.
      3. Else if classic exists → classic (show its failures).
      4. Else if ruleset exists → ruleset (show its failures).
      5. Else → None.
    """
    if classic_eval.passes_policy:
        return "classic", classic_eval
    if ruleset_eval.passes_policy:
        return "ruleset", ruleset_eval
    if classic_eval.exists:
        return "classic", classic_eval
    if ruleset_eval.exists:
        return "ruleset", ruleset_eval
    return None, None


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

        # 2. Branch protection on the default branch — classic OR ruleset.
        #    Evaluate both systems independently, then pick the source that
        #    satisfies policy (preferring classic on a tie). The selected
        #    system supplies the per-policy sub-checks below. Auth identity
        #    and the identity-match check above already ran, so an identity
        #    mismatch is surfaced ahead of protection failures.
        classic_eval = _evaluate_classic_protection(client, owner, repo, result.default_branch)
        ruleset_eval = _evaluate_ruleset_protection(client, owner, repo, result.default_branch)

        source, chosen = _select_protection_source(classic_eval, ruleset_eval)
        if source is None or chosen is None:
            # Neither system has protection on this branch.
            errs = [e for e in (classic_eval.http_error, ruleset_eval.http_error) if e]
            if errs:
                detail = (
                    f"no readable branch protection on `{result.default_branch}` — "
                    "neither classic nor ruleset endpoint returned a result ("
                    + "; ".join(errs)
                    + ")"
                )
            else:
                detail = (
                    f"no branch protection rule on `{result.default_branch}` — "
                    f"neither classic protection nor a ruleset applies; "
                    f"set one up at github.com/{owner}/{repo}/settings/branches"
                )
            result.checks.append(CheckResult("branch protection exists", False, detail))
            return result  # short-circuit; sub-rules are meaningless without protection

        # Top-level "exists" check, annotated with which system passed.
        result.checks.append(CheckResult("branch protection exists", True, f"source: {source}"))
        for sub in chosen.sub_checks:
            result.checks.append(sub)

        # `prot` is only populated when classic is the display source; the
        # required_signatures / required_status_checks warnings below are
        # classic-specific signals and are skipped when ruleset is the source.
        # `classic_eval.raw` is typed `dict | list` (the unified evaluator
        # field) but the classic path always stores a dict — the runtime
        # check keeps mypy happy without a cast.
        prot: dict = (
            classic_eval.raw if source == "classic" and isinstance(classic_eval.raw, dict) else {}
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
            # The branch-protection check stashes "source: classic" or
            # "source: ruleset" in detail; render it in parens so the line
            # reads cleanly: "✓ branch protection exists (source: classic)".
            if c.detail.startswith("source:"):
                line += f" ({c.detail})"
            else:
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
