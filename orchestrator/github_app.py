"""GitHub App identity for agent workers.

When all three of:
  - GITHUB_APP_ID
  - GITHUB_APP_INSTALLATION_ID
  - GITHUB_APP_PRIVATE_KEY_PATH  (or GITHUB_APP_PRIVATE_KEY inline PEM)

are set in env, the orchestrator mints short-lived (1-hour) installation
tokens and gives those to spawned worker agents instead of the user's PAT.

Falls back transparently to GITHUB_TOKEN (PAT) when the App config isn't
present — single-developer sandbox use stays a one-token setup.

Benefits over PAT:
- Commits + PRs attributed to ``<app-name>[bot]`` rather than your user
- Tokens are 1-hour-lived; revoking the App nukes them everywhere
- Scope is per-installation (per-repo or per-org), not per-user
- Audit log shows the bot, not you

What this does NOT fix:
- The self-approval rule. A reviewer agent using the same App identity
  that opened the PR still can't ``gh pr review --approve``. A
  two-identity setup (App for coder/tester, PAT or second App for
  reviewer) is the proper fix — tracked in BACKLOG.md.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
import jwt

# Cache the minted installation token so we don't make a new API call
# every spawn. Installation tokens last 1 hour; we refresh at 50 min to
# leave a safety margin for the agent's wall-clock.
_TOKEN_CACHE_TTL_SECONDS = 50 * 60  # 50 minutes
_token_cache: dict[str, float | str] = {}
_token_lock = threading.Lock()


# --------------------------- env probes ---------------------------


def is_app_configured() -> bool:
    """Return True iff App credentials are fully present in env."""
    if not os.getenv("GITHUB_APP_ID", "").strip():
        return False
    if not os.getenv("GITHUB_APP_INSTALLATION_ID", "").strip():
        return False
    has_path = bool(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip())
    has_inline = bool(os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip())
    return has_path or has_inline


def auth_mode() -> str:
    """Return 'app' or 'pat' or 'none', for logging / doctor output."""
    if is_app_configured():
        return "app"
    if os.getenv("GITHUB_TOKEN", "").strip():
        return "pat"
    return "none"


# --------------------------- private key loading ---------------------------


def _load_private_key() -> str:
    """Read the PEM from either GITHUB_APP_PRIVATE_KEY_PATH or _KEY env var."""
    pem_path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if pem_path:
        return Path(pem_path).expanduser().read_text()
    pem_inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "").strip()
    if pem_inline:
        # Allow \n-escaped single-line inline format from .env
        return pem_inline.replace("\\n", "\n")
    raise RuntimeError(
        "GitHub App private key not configured "
        "(set GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY)"
    )


# --------------------------- JWT + token mint ---------------------------


def _create_app_jwt() -> str:
    """Sign a short-lived JWT proving we are the configured GitHub App.

    Used once to exchange for an installation token; not given to agents.
    """
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    if not app_id:
        raise RuntimeError("GITHUB_APP_ID not set")

    now = int(time.time())
    payload = {
        # iat 60s in the past tolerates minor clock drift between us and GitHub
        "iat": now - 60,
        # 10-minute lifetime is GitHub's maximum
        "exp": now + 600,
        "iss": app_id,
    }
    return jwt.encode(payload, _load_private_key(), algorithm="RS256")


def mint_installation_token() -> str:
    """Exchange the App JWT for a 1-hour installation token. Hits GitHub.

    Uncached. Callers should prefer `get_agent_token()` which caches.
    """
    installation_id = os.getenv("GITHUB_APP_INSTALLATION_ID", "").strip()
    if not installation_id:
        raise RuntimeError("GITHUB_APP_INSTALLATION_ID not set")

    app_jwt = _create_app_jwt()
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-orchestrator",
    }
    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, headers=headers)
        r.raise_for_status()
        return r.json()["token"]


# --------------------------- public API ---------------------------


def get_agent_token() -> str:
    """Return the token to embed in worker agent task messages.

    Order of preference:
      1. App installation token (if GITHUB_APP_* configured) — cached for 50 min
      2. GITHUB_TOKEN PAT (if set)

    Raises RuntimeError if neither is configured.
    """
    if is_app_configured():
        return _cached_installation_token()
    pat = os.getenv("GITHUB_TOKEN", "").strip()
    if pat:
        return pat
    raise RuntimeError(
        "No GitHub auth configured. Set GITHUB_APP_ID + "
        "GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY_PATH "
        "(recommended), or GITHUB_TOKEN (PAT fallback)."
    )


def _cached_installation_token() -> str:
    """Return a cached installation token, minting fresh if expired.

    Cache key is (app_id, installation_id) — changing either invalidates.
    """
    app_id = os.getenv("GITHUB_APP_ID", "").strip()
    installation_id = os.getenv("GITHUB_APP_INSTALLATION_ID", "").strip()
    key = f"{app_id}:{installation_id}"

    with _token_lock:
        now = time.time()
        cached_at = _token_cache.get(f"{key}:ts")
        cached_token = _token_cache.get(f"{key}:token")
        if (
            isinstance(cached_token, str)
            and isinstance(cached_at, int | float)
            and now - cached_at < _TOKEN_CACHE_TTL_SECONDS
        ):
            return cached_token

        token = mint_installation_token()
        _token_cache[f"{key}:token"] = token
        _token_cache[f"{key}:ts"] = now
        return token


def clear_token_cache() -> None:
    """Drop any cached installation token. Forces a fresh mint on next use."""
    with _token_lock:
        _token_cache.clear()
