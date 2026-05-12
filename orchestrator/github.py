"""GitHub REST API helpers used by the orchestrator (NOT by worker agents).

Workers do their own `gh` / `git` work inside their Managed Agent containers.
This module is for orchestrator-side operations: amending PRs after spawn,
polling check status, detecting merges, etc.

Uses httpx (already a transitive dep via anthropic). Auth via GITHUB_TOKEN
from the environment.
"""

from __future__ import annotations

import os
import re

import httpx

_REPO_URL_RE = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
_API_BASE = "https://api.github.com"


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a https://github.com/owner/repo URL."""
    m = _REPO_URL_RE.match(repo_url)
    if not m:
        raise ValueError(f"Could not parse owner/repo from {repo_url!r}")
    return m.group(1), m.group(2)


def _headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set in environment")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-orchestrator",
    }


def amend_pr_body(repo_url: str, pr_number: int, append_text: str) -> None:
    """Append `append_text` to the PR body (separated by a horizontal rule).

    Idempotent-ish: re-running with the same append_text will produce a
    second copy. Callers should de-dupe if they need exactly-once semantics.
    """
    owner, repo = parse_repo_url(repo_url)
    url = f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    with httpx.Client(timeout=15.0, headers=_headers()) as client:
        r = client.get(url)
        r.raise_for_status()
        current_body = r.json().get("body") or ""
        new_body = current_body + "\n\n---\n" + append_text
        r = client.patch(url, json={"body": new_body})
        r.raise_for_status()


def post_pr_comment(repo_url: str, pr_number: int, body: str) -> None:
    """Post a comment on the PR's conversation timeline (issue comment, not review).

    Used by the orchestrator to surface tester/reviewer findings on the PR
    so the human watching github.com sees what happened.
    """
    owner, repo = parse_repo_url(repo_url)
    url = f"{_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    with httpx.Client(timeout=15.0, headers=_headers()) as client:
        r = client.post(url, json={"body": body})
        r.raise_for_status()


def get_pr_state(repo_url: str, pr_number: int) -> dict:
    """Fetch the PR's open/closed/merged state + latest head SHA.

    Returns dict with: state ('open'|'closed'), merged (bool), merged_at, head_sha, mergeable.
    """
    owner, repo = parse_repo_url(repo_url)
    url = f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    with httpx.Client(timeout=15.0, headers=_headers()) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    return {
        "state": data.get("state"),
        "merged": data.get("merged", False),
        "merged_at": data.get("merged_at"),
        "head_sha": data.get("head", {}).get("sha"),
        "mergeable": data.get("mergeable"),
        "mergeable_state": data.get("mergeable_state"),
    }


def request_copilot_review(repo_url: str, pr_number: int) -> dict:
    """Ask GitHub Copilot to review the PR.

    Idempotent-ish: if Copilot already auto-reviewed (because the repo has
    auto-review enabled) or has been requested, GitHub returns 422. We
    treat 422 as success — the review either exists or is on its way.

    Returns {requested: bool, status_code: int, note: str}.
    Never raises; non-Copilot review path should keep working even if this
    fails (e.g. Copilot not available on the repo's plan).
    """
    owner, repo = parse_repo_url(repo_url)
    url = f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers"
    try:
        with httpx.Client(timeout=15.0, headers=_headers()) as client:
            r = client.post(url, json={"reviewers": ["Copilot"]})
        if r.status_code in (200, 201):
            return {"requested": True, "status_code": r.status_code, "note": "requested"}
        if r.status_code == 422:
            # Already requested OR Copilot already reviewed OR PR author
            # mismatch — treat as "either way Copilot is/will be on it"
            return {
                "requested": False,
                "status_code": 422,
                "note": "already requested or already reviewed",
            }
        return {
            "requested": False,
            "status_code": r.status_code,
            "note": f"unexpected status: {r.text[:200]}",
        }
    except Exception as e:
        return {"requested": False, "status_code": 0, "note": f"error: {e}"}


def get_copilot_review(repo_url: str, pr_number: int) -> dict | None:
    """Return the most recent Copilot review on the PR, or None.

    Identifies Copilot by the bot login containing 'copilot'.
    Returns dict with: state, body, submitted_at, author, comments (inline).
    """
    owner, repo = parse_repo_url(repo_url)
    reviews_url = f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
    inline_url = f"{_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
    try:
        with httpx.Client(timeout=15.0, headers=_headers()) as client:
            r = client.get(reviews_url)
            r.raise_for_status()
            reviews = r.json()
            copilot_reviews = [
                rev
                for rev in reviews
                if "copilot" in (rev.get("user", {}).get("login", "") or "").lower()
            ]
            if not copilot_reviews:
                return None
            # Most recent (last in list, GitHub returns chronological)
            latest = copilot_reviews[-1]

            # Also fetch inline review comments by Copilot
            c = client.get(inline_url)
            c.raise_for_status()
            inline_comments = [
                {"path": cmt.get("path"), "line": cmt.get("line"), "body": cmt.get("body")}
                for cmt in c.json()
                if "copilot" in (cmt.get("user", {}).get("login", "") or "").lower()
            ]

            return {
                "state": latest.get("state"),
                "body": latest.get("body"),
                "submitted_at": latest.get("submitted_at"),
                "author": latest.get("user", {}).get("login"),
                "inline_comments": inline_comments,
                "inline_count": len(inline_comments),
            }
    except Exception:
        return None


def wait_for_copilot_review(
    repo_url: str, pr_number: int, *, timeout_seconds: int = 300, poll_seconds: int = 15
) -> dict | None:
    """Poll until a Copilot review appears, or timeout. Returns the review or None.

    Use after requesting via request_copilot_review. If Copilot is not
    enabled on the repo's plan, returns None after the full timeout.
    Default: 5 minutes total, polled every 15 seconds.
    """
    import time

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        review = get_copilot_review(repo_url, pr_number)
        if review:
            return review
        time.sleep(poll_seconds)
    return None


def get_pr_check_runs(repo_url: str, pr_number: int) -> dict:
    """Fetch CI check_runs for the PR's head commit.

    Returns dict with: total, conclusion_counts (success/failure/...),
    runs (list of {name, status, conclusion, details_url}).

    Useful for the lead/orchestrator to know if CI is green/red/still running
    before spawning the reviewer.
    """
    pr_state = get_pr_state(repo_url, pr_number)
    head_sha = pr_state.get("head_sha")
    if not head_sha:
        return {"total": 0, "conclusion_counts": {}, "runs": []}

    owner, repo = parse_repo_url(repo_url)
    url = f"{_API_BASE}/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
    with httpx.Client(timeout=15.0, headers=_headers()) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()

    runs = data.get("check_runs", [])
    from collections import Counter

    conclusions = Counter(run.get("conclusion") or "in_progress" for run in runs)
    return {
        "total": data.get("total_count", 0),
        "conclusion_counts": dict(conclusions),
        "runs": [
            {
                "name": r.get("name"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "details_url": r.get("details_url"),
            }
            for r in runs
        ],
        "head_sha": head_sha,
    }
