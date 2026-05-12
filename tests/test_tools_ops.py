"""Tests for orchestrator/tools/ops.py — operational MCP tools."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import ops


def _seed_unit(feature_id="F", unit_id="U1", status="coding", **kwargs):
    state.save_feature(
        Feature(id=feature_id, title="t", description="d", repo_path="https://github.com/o/r"),
    )
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            branch="b",
            **kwargs,
        ),
    )


# --------------------------- hello_world_test ---------------------------


def test_hello_world_test_calls_managed_agent(tmp_state_db, monkeypatch):
    """Verifies the smoke test spawns a worker and archives — no real API calls."""
    fake_worker = MagicMock()
    fake_worker.spawn.return_value = ("sesn_fake", "hello from a managed agent")
    fake_worker.archive.return_value = None

    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    out = ops.hello_world_test()
    assert "session_id=sesn_fake" in out
    assert "hello from a managed agent" in out
    fake_worker.spawn.assert_called_once()
    fake_worker.archive.assert_called_once_with("sesn_fake")


# --------------------------- check_unit_pr ---------------------------


def test_check_unit_pr_no_pr(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description=""))
    state.upsert_unit_state(WorkUnitState(unit_id="U1", feature_id="F", status="coding"))
    assert "ERROR" in ops.check_unit_pr("U1")


def test_check_unit_pr_unknown_unit(tmp_state_db):
    assert "no PR" in ops.check_unit_pr("nope")


def test_check_unit_pr_missing_token(tmp_state_db, no_github_token):
    _seed_unit(pr_number=5)
    msg = ops.check_unit_pr("U1")
    assert "no GitHub auth" in msg


def test_check_unit_pr_flips_to_done_when_merged(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="in_ci")

    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-05-11T19:00:00Z",
            "head_sha": "abc",
        },
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    out = ops.check_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["pr_state"]["merged"] is True
    assert parsed["orchestrator_status"] == "done"


def test_check_unit_pr_keeps_status_when_not_merged(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5, status="in_ci")
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_state",
        lambda url, pr: {"state": "open", "merged": False, "head_sha": "abc"},
    )
    monkeypatch.setattr(
        "orchestrator.tools.ops.github.get_pr_check_runs",
        lambda url, pr: {"total": 0, "conclusion_counts": {}, "runs": []},
    )

    out = ops.check_unit_pr("U1")
    parsed = json.loads(out)
    assert parsed["orchestrator_status"] == "in_ci"


def test_check_unit_pr_handles_github_error(tmp_state_db, with_github_token, monkeypatch):
    _seed_unit(pr_number=5)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("orchestrator.tools.ops.github.get_pr_state", boom)

    msg = ops.check_unit_pr("U1")
    assert "ERROR querying GitHub" in msg


# --------------------------- list_in_flight ---------------------------


def test_list_in_flight_empty(tmp_state_db):
    out = ops.list_in_flight()
    parsed = json.loads(out)
    assert parsed == []


def test_list_in_flight_returns_only_active_statuses(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    for uid, status in [
        ("U1", "coding"),
        ("U2", "done"),  # not active
        ("U3", "in_ci"),
        ("U4", "escalated"),  # not active
        ("U5", "reviewing"),
    ]:
        state.upsert_unit_state(WorkUnitState(unit_id=uid, feature_id="F", status=status))

    out = ops.list_in_flight()
    parsed = json.loads(out)
    unit_ids = sorted(r["unit_id"] for r in parsed)
    assert unit_ids == ["U1", "U3", "U5"]


def test_list_in_flight_includes_session_flags(tmp_state_db):
    state.save_feature(Feature(id="F", title="t", description="d"))
    state.upsert_unit_state(
        WorkUnitState(
            unit_id="U1",
            feature_id="F",
            status="reviewing",
            coder_session_id="sesn_c",
            tester_session_id="sesn_t",
        ),
    )
    out = ops.list_in_flight()
    parsed = json.loads(out)
    row = parsed[0]
    assert row["has_coder_session"] is True
    assert row["has_tester_session"] is True
    assert row["has_reviewer_session"] is False


# --------------------------- resume_unit ---------------------------


def test_resume_unit_bad_role(tmp_state_db):
    msg = ops.resume_unit("U1", role="hacker")
    assert "ERROR" in msg
    assert "role must be" in msg


def test_resume_unit_no_state(tmp_state_db):
    msg = ops.resume_unit("nope", role="coder")
    assert "no state" in msg


def test_resume_unit_no_session_id(tmp_state_db):
    _seed_unit()  # no session ids set
    msg = ops.resume_unit("U1", role="coder")
    assert "no coder session_id stored" in msg


def test_resume_unit_returns_session_status(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")

    fake_session = MagicMock(status="idle", title="my session")
    fake_worker = MagicMock()
    fake_worker.client.beta.sessions.retrieve.return_value = fake_session
    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    out = ops.resume_unit("U1", role="coder")
    parsed = json.loads(out)
    assert parsed["session_id"] == "sesn_xyz"
    assert parsed["session_status"] == "idle"
    assert parsed["role"] == "coder"


def test_resume_unit_handles_retrieve_error(tmp_state_db, monkeypatch):
    _seed_unit(coder_session_id="sesn_xyz")
    fake_worker = MagicMock()
    fake_worker.client.beta.sessions.retrieve.side_effect = RuntimeError("expired")
    monkeypatch.setattr("orchestrator.tools.ops.ManagedAgentWorker", lambda role: fake_worker)

    msg = ops.resume_unit("U1", role="coder")
    assert "ERROR retrieving session" in msg


# --------------------------- reset_cached_resources ---------------------------


def test_reset_cached_resources_empty(tmp_state_db):
    msg = ops.reset_cached_resources()
    assert "Cleared 0" in msg


def test_reset_cached_resources_counts_rows(tmp_state_db):
    state.save_cached_resource("coder", "sig1", "agent_1", "env_1")
    state.save_cached_resource("tester", "sig2", "agent_2", "env_2")
    msg = ops.reset_cached_resources()
    assert "Cleared 2" in msg
    assert state.get_cached_resource("coder", "sig1") is None


# --------------------------- verify_repo / list_verified / forget_repo ---------------------------


def _stub_verify_pass(monkeypatch):
    """Patch repo_verify.verify to return a passing result without any HTTP."""
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
            auth_identity="user:tester",
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

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)


def _stub_verify_fail(monkeypatch, detail="no rule on main"):
    from orchestrator.models import CheckResult, VerificationResult

    def fake_verify(url, token, auth_mode="pat"):
        return VerificationResult(
            repo_url="https://github.com/owner/repo",
            default_branch="main",
            auth_mode=auth_mode,
            checks=[CheckResult("branch protection exists", False, detail)],
        )

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", fake_verify)


def _stub_token(monkeypatch):
    monkeypatch.setattr("orchestrator.tools.ops.github_app.get_agent_token", lambda: "ghp_fake")
    monkeypatch.setattr("orchestrator.tools.ops.github_app.auth_mode", lambda: "pat")


def test_verify_repo_caches_on_success(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    assert "✓" in out
    assert "Cached" in out
    cached = state.get_verified_repo("https://github.com/owner/repo")
    assert cached is not None
    assert cached.default_branch == "main"


def test_verify_repo_does_not_cache_on_failure(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_fail(monkeypatch)

    out = ops.verify_repo("github.com/owner/repo")
    assert "FAILED" in out
    assert state.get_verified_repo("https://github.com/owner/repo") is None


def test_verify_repo_missing_token(tmp_state_db, no_github_token):
    out = ops.verify_repo("github.com/owner/repo")
    assert "no GitHub auth" in out.lower() or "github_token" in out.lower()


def test_verify_repo_handles_value_error(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    # Bad URL form — normalize_repo_url raises before verify() is called
    out = ops.verify_repo("git@github.com:owner/repo")
    assert "ERROR" in out
    assert "SSH" in out


def test_verify_repo_surfaces_github_error(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)

    def boom(url, token, auth_mode="pat"):
        raise RuntimeError("network exploded")

    monkeypatch.setattr("orchestrator.tools.ops.repo_verify.verify", boom)
    out = ops.verify_repo("github.com/owner/repo")
    assert "ERROR contacting GitHub" in out
    assert "network exploded" in out


def test_list_verified_repos_empty(tmp_state_db):
    # Clear the fixture's pre-seeded test repos so we exercise the empty path
    state.forget_verified_repo("https://github.com/o/r")
    state.forget_verified_repo("https://github.com/joe/repo")
    out = ops.list_verified_repos()
    assert "No repos have been verified" in out


def test_list_verified_repos_returns_json(tmp_state_db, with_github_token, monkeypatch):
    # Clear the fixture's pre-seeded entries to focus the assertion
    state.forget_verified_repo("https://github.com/o/r")
    state.forget_verified_repo("https://github.com/joe/repo")
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)
    ops.verify_repo("github.com/owner/repo")

    out = ops.list_verified_repos()
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["repo_url"] == "https://github.com/owner/repo"
    assert parsed[0]["default_branch"] == "main"
    assert parsed[0]["auth_mode"] == "pat"


def test_forget_repo_removes_row(tmp_state_db, with_github_token, monkeypatch):
    _stub_token(monkeypatch)
    _stub_verify_pass(monkeypatch)
    ops.verify_repo("github.com/owner/repo")
    assert state.get_verified_repo("https://github.com/owner/repo") is not None

    out = ops.forget_repo("github.com/owner/repo")
    assert "Forgot" in out
    assert state.get_verified_repo("https://github.com/owner/repo") is None


def test_forget_repo_missing_url(tmp_state_db):
    out = ops.forget_repo("github.com/never/seen")
    assert "was not in the cache" in out


def test_forget_repo_rejects_bad_url(tmp_state_db):
    out = ops.forget_repo("not a url at all")
    assert "ERROR" in out
