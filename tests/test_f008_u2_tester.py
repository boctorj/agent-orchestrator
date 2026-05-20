"""Tester-written contracts for F-008-U-2 (``tail_worker`` MCP tool).

These tests complement (rather than duplicate) the coder's
``tests/test_tools_ops.py`` test suite for ``tail_worker``. They lock the
behaviours that the unit description names explicitly but the coder's
suite either covers implicitly or not at all:

  * **Integration with the MCP registry.** The unit description says
    "Register MCP tool" — the coder's tests exercise the underlying
    Python function via ``ops.tail_worker(...)`` but never assert that
    the tool actually shows up in ``mcp.list_tools()``. A future refactor
    that dropped the ``@mcp.tool()`` decorator would leave every coder
    test green while the production MCP server silently lost the tool.

  * **CLAUDE.md persona doc updated** (explicit deliverable in the unit
    description). The doc is the *runtime persona* loaded when Claude
    Code launches via ``orchestrator run``; missing the new tool from
    the persona docs means the lead never knows to call it.

  * **Role → session_id mapping is per-role.** The coder's tests pass
    a coder_session_id and a tester role separately, but never assert
    that ``tail_worker(unit_id, role='tester')`` resolves the
    ``tester_session_id`` field (not the coder one). A subtle bug
    where the role argument was thrown away and ``coder_session_id``
    was always read would still let single-role tests pass.

  * **``make_worker`` factory is called with the requested role.** Same
    failure mode as above — the coder's tests mock ``make_worker``
    with ``lambda role: fake_worker`` and never look at what role was
    passed in. Backend selection (managed_agents vs docker) is per
    ``ORCH_WORKER_BACKEND``; the role argument is what plumbs the
    correct prompt and identity through ``make_worker``.

  * **Status-aware formatting strings match the spec verbatim.** The
    coder's tests assert "worker active" / "worker completed" /
    "worker dead" substrings but never pin the full per-status phrasing
    from the unit description. A regression that flipped "worker
    completed, final messages" → "worker finished, last messages"
    would still pass the substring tests.

  * **Read-only contract** — already covered by the coder, kept here as
    a pinned cross-check because the unit description's "Read-only (no
    state.db writes, no session perturbation, no events)" claim is
    load-bearing for the headless daemon phase.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from orchestrator import state
from orchestrator.models import Feature, WorkUnitState
from orchestrator.tools import mcp, ops


# ---------------------------------------------------------------------------
# Helpers — mirror the coder's _seed_unit so tests read uniformly.
# ---------------------------------------------------------------------------


def _seed_unit(feature_id: str = "F", unit_id: str = "U1", status: str = "coding", **kwargs):
    """Seed a feature + unit row mirroring the coder's helper.

    Kept separate so this file doesn't import from ``tests/test_tools_ops.py``
    (private helper coupling would let the coder's refactor break this
    file silently).
    """
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


def _fake_worker_returning(status: str, messages: list[dict], reason: str | None = None):
    fake = MagicMock()
    fake.tail_messages.return_value = {
        "status": status,
        "messages": messages,
        "reason": reason,
    }
    return fake


# ---------------------------------------------------------------------------
# 1. Integration: tail_worker is registered with FastMCP under that name.
# ---------------------------------------------------------------------------


def test_tail_worker_is_registered_as_mcp_tool():
    """The unit description says "Register MCP tool" — assert it actually
    shows up in the FastMCP registry under the spec name. A future
    refactor that drops the ``@mcp.tool()`` decorator on ``tail_worker``
    would leave every behavioural test green while the production MCP
    server silently lost the tool.
    """
    # Importing ``ops`` already triggers the @mcp.tool() decorator side
    # effects above; force-import explicitly so the test is robust to
    # import-order changes.
    import orchestrator.tools.ops  # noqa: F401  (re-import for clarity)

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "tail_worker" in names, (
        f"tail_worker missing from MCP tool registry — found: {sorted(names)}"
    )


def test_tail_worker_mcp_signature_advertises_unit_id_role_limit():
    """The registered tool's parameter schema must include the three
    arguments named in the unit title: ``unit_id``, ``role``, ``limit``.
    Drift in the signature (e.g. accidentally dropping ``limit`` to a
    private default) would silently change the LLM's available
    surface."""
    import orchestrator.tools.ops  # noqa: F401

    tools = asyncio.run(mcp.list_tools())
    tail = next(t for t in tools if t.name == "tail_worker")
    schema = tail.inputSchema or {}
    properties = schema.get("properties", {})
    assert "unit_id" in properties
    assert "role" in properties
    assert "limit" in properties


# ---------------------------------------------------------------------------
# 2. CLAUDE.md persona doc updated.
# ---------------------------------------------------------------------------


def _claude_md_text() -> str:
    """Read CLAUDE.md as a single string for substring assertions.

    Located via the repo root one parent above ``tests/``. The persona
    doc is checked in at the repo root by convention (see
    ``CONTRIBUTING.md`` § "Repo layout").
    """
    root = Path(__file__).resolve().parent.parent
    return (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_claude_md_persona_documents_tail_worker_signature():
    """The persona must advertise the tool by name + signature so the
    lead knows when to call it. Catches the case where the impl ships
    but the persona never learns the new vocabulary.
    """
    text = _claude_md_text()
    assert "tail_worker" in text
    # The signature in the unit title is the canonical form for the persona.
    assert "tail_worker(unit_id, role" in text


def test_claude_md_persona_describes_all_four_statuses():
    """Each of the four ``TailStatus`` values must show up in the persona
    so the lead can interpret the tool's output. Missing one (e.g.
    ``not_found``) would make the lead confused about a real-world
    response from that branch.
    """
    text = _claude_md_text()
    for status in ("running", "idle", "terminated", "not_found"):
        assert status in text, f"persona doc is missing the {status!r} branch guidance"


def test_claude_md_persona_terminated_guidance_points_to_resume_unit_and_escalation():
    """The unit description explicitly says: "terminated should be
    followed up with resume_unit + escalation". The persona must
    encode that follow-up so the lead knows what to do when the
    backend reports a dead session.
    """
    text = _claude_md_text()
    # Find the tail_worker section + its terminated bullet.
    assert "tail_worker" in text
    # Strict substring search across the whole file — the persona may
    # phrase the guidance in either the tool-definition section or the
    # restart-recovery flow section; either is fine, but both must
    # mention resume_unit + escalation as the follow-up.
    lower = text.lower()
    assert "resume_unit" in text, "persona must mention resume_unit as the terminated follow-up"
    assert "escalat" in lower, "persona must mention escalation as the terminated follow-up"


def test_claude_md_persona_describes_when_to_call():
    """Description says "usage guidance (when to call ...)". The persona
    needs concrete trigger conditions, not just a tool-signature blurb.
    """
    text = _claude_md_text().lower()
    # At least one of the canonical when-to-call cues must appear.
    cues = ["blocking", "what's the coder doing", "triag", "hung", "progress"]
    assert any(cue in text for cue in cues), (
        "persona doc must include at least one 'when to call' usage cue "
        f"out of {cues}"
    )


# ---------------------------------------------------------------------------
# 3. Role → session_id resolution is per-role.
# ---------------------------------------------------------------------------


def test_tail_worker_role_coder_uses_coder_session_id(tmp_state_db, monkeypatch):
    """Each role must read its own session_id field — a regression that
    routed every role through ``coder_session_id`` would still pass
    single-role tests because the seeded id happens to match the
    field the bug reads."""
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="coder")

    # Must be called with the coder session, not tester / reviewer.
    fake.tail_messages.assert_called_once_with("sesn_coder", limit=20)


def test_tail_worker_role_tester_uses_tester_session_id(tmp_state_db, monkeypatch):
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="tester")

    fake.tail_messages.assert_called_once_with("sesn_tester", limit=20)


def test_tail_worker_role_reviewer_uses_reviewer_session_id(tmp_state_db, monkeypatch):
    _seed_unit(
        coder_session_id="sesn_coder",
        tester_session_id="sesn_tester",
        reviewer_session_id="sesn_reviewer",
    )
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    ops.tail_worker("U1", role="reviewer")

    fake.tail_messages.assert_called_once_with("sesn_reviewer", limit=20)


def test_tail_worker_role_isolation_missing_role_session_does_not_fall_back(
    tmp_state_db, monkeypatch
):
    """If the role's own session_id is empty but other roles' ids ARE
    set, ``tail_worker`` must report "no session for unit_id/role" —
    NOT silently fall back to whichever role has a stored id.
    """
    _seed_unit(coder_session_id="", tester_session_id="sesn_t", reviewer_session_id="sesn_r")

    # Patch make_worker so a buggy fall-through would be visible: the
    # fake would be called and we'd see the call recorded.
    fake = _fake_worker_returning("running", [])
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    msg = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder" in msg
    assert "never spawned" in msg
    # And critically: make_worker must NOT have been invoked, because
    # the no-session branch should short-circuit before backend lookup.
    fake.tail_messages.assert_not_called()


# ---------------------------------------------------------------------------
# 4. make_worker factory is called with the requested role (not hardcoded).
# ---------------------------------------------------------------------------


def test_tail_worker_factory_called_with_requested_role(tmp_state_db, monkeypatch):
    """The factory dispatches to managed-agents / docker; the role
    argument is what selects the correct prompt + identity per backend.
    A regression where ``make_worker`` was always called with
    ``role='coder'`` (or with the unit_id) would silently break
    tester/reviewer worker semantics."""
    _seed_unit(tester_session_id="sesn_t")
    captured: dict[str, str] = {}

    def fake_factory(role: str):
        captured["role"] = role
        return _fake_worker_returning("running", [])

    monkeypatch.setattr("orchestrator.tools.ops.make_worker", fake_factory)
    ops.tail_worker("U1", role="tester")

    assert captured["role"] == "tester"


# ---------------------------------------------------------------------------
# 5. Status-aware formatting — verbatim spec strings.
# ---------------------------------------------------------------------------


def test_tail_worker_running_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec: running → "worker active, last N messages". Pin the comma
    + the noun phrase + the message count rendering verbatim — the
    coder's substring tests miss e.g. a regression that dropped the
    comma or pluralised differently.
    """
    _seed_unit(coder_session_id="sesn_xyz")
    msgs = [
        {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "one"},
        {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "two"},
        {"ts": "2025-01-01T12:00:02Z", "role": "agent", "text": "three"},
    ]
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning("running", msgs),
    )

    out = ops.tail_worker("U1", role="coder")
    # Exact spec phrasing with the N substituted.
    assert "worker active, last 3 messages" in out


def test_tail_worker_idle_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec: idle → "worker completed, final messages"."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "idle",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "TESTS_PASS"}],
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "worker completed, final messages" in out


def test_tail_worker_terminated_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec: terminated → "worker dead (reason); last messages before
    death". The semicolon + "before death" tail are part of the spec
    phrasing and must be preserved (the coder's tests only check for
    "worker dead" / "before death" individually)."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "terminated",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "oom"}],
            reason="container exit 137",
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    # The full spec phrase with reason interpolated:
    assert "worker dead (container exit 137); last messages before death" in out


def test_tail_worker_not_found_format_matches_spec_phrase(tmp_state_db, monkeypatch):
    """Spec: not_found → "no session for unit_id/role - likely never
    spawned". Pin the slash-separated unit_id/role and the "likely
    never spawned" tail.
    """
    _seed_unit(coder_session_id="sesn_dead")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning("not_found", []),
    )

    out = ops.tail_worker("U1", role="coder")
    assert "no session for U1/coder" in out
    assert "likely never spawned" in out


# ---------------------------------------------------------------------------
# 6. Read-only contract — pin alongside ops.py docstring claim.
# ---------------------------------------------------------------------------


def test_tail_worker_does_not_mutate_unit_state_or_emit_events(tmp_state_db, monkeypatch):
    """Unit description (paraphrased): "Read-only — no state.db writes,
    no session perturbation, no events." Pin all three.
    """
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "running",
            [{"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "x"}],
        ),
    )

    pre_state = state.get_unit_state("U1")
    pre_events = state.list_events("U1")

    # Call multiple times to amplify any per-call drift.
    for _ in range(3):
        ops.tail_worker("U1", role="coder")

    post_state = state.get_unit_state("U1")
    assert post_state.status == pre_state.status
    assert post_state.last_activity == pre_state.last_activity
    assert post_state.last_error == pre_state.last_error
    assert state.list_events("U1") == pre_events


# ---------------------------------------------------------------------------
# 7. Messages rendered with [ts] role: text shape (cross-check format).
# ---------------------------------------------------------------------------


def test_tail_worker_message_lines_render_ts_role_text(tmp_state_db, monkeypatch):
    """Each rendered message line should carry the ts, role, and text
    so the lead can correlate against ``unit_history`` timestamps."""
    _seed_unit(coder_session_id="sesn_xyz")
    monkeypatch.setattr(
        "orchestrator.tools.ops.make_worker",
        lambda role: _fake_worker_returning(
            "running",
            [
                {"ts": "2025-01-01T12:00:00Z", "role": "agent", "text": "alpha"},
                {"ts": "2025-01-01T12:00:01Z", "role": "agent", "text": "beta"},
            ],
        ),
    )

    out = ops.tail_worker("U1", role="coder")
    # ts + role + text all present per line (the exact separator is
    # an implementation detail; assert co-occurrence of all three on a
    # single line for each message).
    lines = out.splitlines()
    msg_lines = [ln for ln in lines if "alpha" in ln or "beta" in ln]
    assert len(msg_lines) == 2
    for ln in msg_lines:
        assert "2025-01-01T12:00:0" in ln
        assert "agent" in ln


# ---------------------------------------------------------------------------
# 8. Error-handling — backend exceptions surface as ERROR; ValueError
#    surfaces separately. Pin both branches.
# ---------------------------------------------------------------------------


def test_tail_worker_value_error_from_backend_surfaces_as_error(tmp_state_db, monkeypatch):
    """The protocol layer raises ValueError for ``limit < 1``; the MCP
    tool must surface it as a chat-friendly ERROR string, not let it
    propagate into the MCP loop."""
    _seed_unit(coder_session_id="sesn_xyz")

    def raise_value_error(*a, **kw):
        raise ValueError("tail_messages limit must be an int >= 1, got 0")

    fake = MagicMock()
    fake.tail_messages.side_effect = raise_value_error
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    msg = ops.tail_worker("U1", role="coder", limit=0)
    assert msg.startswith("ERROR")
    # Lead should see the precondition violation in the chat.
    assert "limit" in msg


def test_tail_worker_generic_exception_does_not_crash(tmp_state_db, monkeypatch):
    """The unit description's "observability tool must not crash chat"
    claim — any non-ValueError backend exception is surfaced as
    ERROR rather than propagated."""
    _seed_unit(coder_session_id="sesn_xyz")

    fake = MagicMock()
    fake.tail_messages.side_effect = RuntimeError("boom")
    monkeypatch.setattr("orchestrator.tools.ops.make_worker", lambda role: fake)

    # Must not raise.
    msg = ops.tail_worker("U1", role="coder")
    assert msg.startswith("ERROR")
    assert "boom" in msg


def test_tail_worker_invalid_role_returns_actionable_error(tmp_state_db):
    """Bad role — the lead needs to know which roles are valid."""
    msg = ops.tail_worker("U1", role="ceo")
    assert msg.startswith("ERROR")
    # The valid options must be listed so the lead can correct.
    for valid in ("coder", "tester", "reviewer"):
        assert valid in msg
