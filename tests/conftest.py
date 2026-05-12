"""Shared pytest fixtures.

Reads-only — none of these fixtures touch the real `state.db` in the
project root. Every test runs against a temporary SQLite file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_state_db(monkeypatch, tmp_path: Path):
    """Redirect state.STATE_DB to a temporary file and initialize the schema.

    Pre-seeds the verified_repos cache with the URLs used by existing test
    fixtures (`https://github.com/o/r`, `https://github.com/joe/repo`) so
    that gate-protected spawn calls don't all need to wire verification by
    hand. Tests that specifically exercise the unverified-repo gate should
    delete those rows via `state.forget_verified_repo(url)`.

    Yields the path so tests can poke the raw DB if they need to. Imports
    happen inside the fixture so monkeypatch lands before any module
    captures STATE_DB by value.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("orchestrator.state.STATE_DB", db_path)

    from orchestrator import state
    from orchestrator.models import CheckResult, VerificationResult

    state.init_db()

    # Pre-verify the standard test repos so existing tests don't need to
    # care about the verification gate. Keep this list in sync with the
    # repo_path values used in test fixtures across tests/test_tools_*.py.
    def _passing(url: str) -> VerificationResult:
        return VerificationResult(
            repo_url=url,
            default_branch="main",
            auth_mode="pat",
            auth_identity="user:test-fixture",
            checks=[
                CheckResult("read access", True),
                CheckResult("write access", True),
                CheckResult("branch protection exists", True),
                CheckResult(
                    "≥1 approving review required",
                    True,
                    "required_approving_review_count = 1",
                ),
                CheckResult("force push blocked", True),
                CheckResult("deletion blocked", True),
                CheckResult("admin bypass blocked", True),
            ],
        )

    for url in ("https://github.com/o/r", "https://github.com/joe/repo"):
        state.save_verified_repo(_passing(url))

    yield db_path


@pytest.fixture
def no_github_token(monkeypatch):
    """Ensure no GitHub auth (PAT or App) is configured.

    Without this, App env vars from a developer's shell could make
    `need_github_token()` pass when the test expects it to fail.
    """
    for var in (
        "GITHUB_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def with_github_token(monkeypatch):
    """Set a fake GITHUB_TOKEN (PAT path) and clear App env so PAT wins."""
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_fake_for_tests")
    for var in (
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_ntfy_topic(monkeypatch):
    """Ensure NTFY_TOPIC is unset so push() no-ops cleanly."""
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture
def with_ntfy_topic(monkeypatch):
    """Set a fake NTFY_TOPIC."""
    monkeypatch.setenv("NTFY_TOPIC", "test-topic-do-not-use")
