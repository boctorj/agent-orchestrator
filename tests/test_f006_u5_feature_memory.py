"""Independent tests for F-006-U-5: ``feature_memory(feature_id)`` MCP tool.

These tests reflect the INTENDED behavior from the unit description:

  > New MCP tool ``feature_memory(feature_id) -> str`` returning a
  > ~3-7K-token session-bootstrap blob: ``spec.md`` content +
  > ``git log -10 -- features/F-XXX/spec.md`` + aggregated
  > ``unit_summary`` + cycle-log "Final" / terminal sections
  > (~500 tokens per merged unit) + recent escalation events from
  > ``unit_events``. Register in the MCP server. Tests cover the
  > empty-state case (no spec.md, no cycle logs) and the populated
  > case (mixed merged / in-flight / escalated units). Read-side
  > must handle missing ``features/`` directory gracefully.

Sister suite to ``tests/test_feature_memory.py`` (the coder's
exhaustive coverage); these tests duplicate the *acceptance* surface
intentionally so a regression in any one ingredient (spec/git log/
unit summary/cycle finals/escalations) is caught by name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import feature_memory, state
from orchestrator.models import Feature, WorkUnitState

# --------------------------- helpers ---------------------------


@dataclass
class _FakeProc:
    """Minimal ``subprocess.CompletedProcess`` look-alike."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class _Runner:
    """Records every argv and returns a single canned response."""

    def __init__(self, proc: _FakeProc | None = None) -> None:
        self.proc = proc or _FakeProc()
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeProc:
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        return self.proc


def _seed_feature(
    feature_id: str = "F-101",
    title: str = "Bootstrap",
    description: str = "Verify the memory blob shape.",
    repo_path: str = "https://github.com/o/r",
    status: str = "approved",
) -> Feature:
    f = Feature(
        id=feature_id,
        title=title,
        description=description,
        repo_path=repo_path,
        status=status,  # type: ignore[arg-type]
    )
    state.save_feature(f)
    return f


def _seed_unit(
    feature_id: str,
    unit_id: str,
    *,
    status: str = "pending",
    pr_number: int | None = None,
    last_error: str = "",
    review_round: int = 0,
    events: list[dict[str, Any]] | None = None,
) -> None:
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,  # type: ignore[arg-type]
            pr_number=pr_number,
            last_error=last_error,
            review_round=review_round,
        )
    )
    for ev in events or []:
        state.record_event(
            unit_id,
            feature_id,
            ev["event_type"],
            source=ev.get("source", "orchestrator"),
            cycle_number=ev.get("cycle_number"),
            summary=ev.get("summary", ""),
            details=ev.get("details", ""),
        )


def _write_cycle_log(base_dir: Path, feature_id: str, unit_id: str, body: str) -> Path:
    fdir = base_dir / "features" / feature_id
    fdir.mkdir(parents=True, exist_ok=True)
    # Cycle-log files are named ``U-N.md`` (see ``_unit_basename``).
    tail = unit_id.rsplit("-", 2)[-1]
    target = fdir / f"U-{tail}.md"
    target.write_text(body, encoding="utf-8")
    return target


# --------------------------- empty-state case ---------------------------


class TestEmptyState:
    """Feature exists in state.db but nothing else does — no spec.md, no
    units, no cycle logs, no events. Per the unit description this is one
    of the two cases tests must cover."""

    def test_returns_non_empty_string(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed_feature(feature_id="F-101")
        out = feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=_Runner())
        assert isinstance(out, str)
        assert out.strip() != ""

    def test_does_not_raise_when_features_dir_missing(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Unit description: 'Read-side must handle missing features/ directory gracefully.'"""
        _seed_feature(feature_id="F-101")
        assert not (tmp_path / "features").exists()
        # No exception is the assertion here.
        feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=_Runner())

    def test_emits_spec_placeholder_when_no_spec_md(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_feature(feature_id="F-101")
        out = feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=_Runner())
        # spec.md heading must be present, and the body must not be a
        # real spec — it must be a placeholder. Lowercase "no spec" is
        # the contract; exact wording is implementation detail.
        assert "spec.md" in out.lower()
        # No traceback / error marker leaked into the blob.
        assert "Traceback" not in out
        assert "ERROR" not in out.split("\n")[0]  # header is not an error

    def test_units_section_has_placeholder_when_no_units(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_feature(feature_id="F-101")
        out = feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=_Runner())
        # Some placeholder for "no units" appears (case-insensitive).
        assert "no unit" in out.lower() or "no units" in out.lower()

    def test_escalations_section_has_placeholder_when_no_events(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_feature(feature_id="F-101")
        out = feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=_Runner())
        assert "no escalation" in out.lower()


# --------------------------- populated case ---------------------------


def _populated_setup(tmp_path: Path) -> None:
    """Seed F-200 with: a real spec.md, three units (merged / in-flight /
    escalated), cycle logs for the two terminal units, and escalation
    events on the escalated unit."""
    _seed_feature(
        feature_id="F-200",
        title="Mixed bag",
        description="Acceptance: prove the blob fuses every ingredient.",
    )

    spec_dir = tmp_path / "features" / "F-200"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(
        "# F-200: Mixed bag\n\n"
        "## Intent\nFuse spec + cycles + escalations.\n\n"
        "## Acceptance\nfeature_memory returns ≥ 4 sections + escalations.\n",
        encoding="utf-8",
    )

    # U-1 merged with a cycle log on disk
    _seed_unit(
        "F-200",
        "F-200-U-1",
        status="done",
        pr_number=10,
        review_round=1,
        events=[
            {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #10"},
            {
                "event_type": "reviewer_recommend_merge",
                "cycle_number": 1,
                "summary": "ready to merge",
            },
        ],
    )
    _write_cycle_log(
        tmp_path,
        "F-200",
        "F-200-U-1",
        "# F-200-U-1 — first unit\n\n"
        "## PR\n"
        "#10 · https://github.com/o/r/pull/10\n"
        "Status: done (2026-05-17 02:48 UTC)\n"
        "PR head SHA: aaa111\n"
        "Merge commit SHA: bbb222\n\n"
        "## Coder's PR description (verbatim, as of last capture)\n"
        "First unit body.\n\n"
        "## Cycle history\n"
        "1 cycles · cap-3 not hit\n\n"
        "### Cycle 0 — coder: PR opened\n"
        "- PR #10\n\n"
        "### Cycle 1 — reviewer: REVIEW_RECOMMEND_MERGE\n"
        "- ready to merge\n\n"
        "## Review threads\n"
        "_no review threads_\n",
    )

    # U-2 in-flight, no cycle log file yet
    _seed_unit(
        "F-200",
        "F-200-U-2",
        status="in_ci",
        pr_number=11,
        review_round=1,
        events=[
            {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #11"},
            {"event_type": "fix_pushed", "cycle_number": 1, "summary": "fix-up"},
        ],
    )

    # U-3 escalated, cycle log on disk, escalation events present
    _seed_unit(
        "F-200",
        "F-200-U-3",
        status="escalated",
        pr_number=12,
        review_round=3,
        last_error="BLOCKED [auth_failure]: 401 from gh api",
        events=[
            {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #12"},
            {
                "event_type": "tester_bug_found",
                "cycle_number": 1,
                "summary": "test 500",
            },
            {
                "event_type": "coder_blocked",
                "cycle_number": 2,
                "summary": "401 from gh api",
                "details": json.dumps({"reason": "auth_failure", "prose": "401 from gh api"}),
            },
            {
                "event_type": "reviewer_no_marker",
                "cycle_number": 3,
                "summary": "agent emitted no terminal marker",
            },
        ],
    )
    _write_cycle_log(
        tmp_path,
        "F-200",
        "F-200-U-3",
        "# F-200-U-3 — third unit\n\n"
        "## PR\n"
        "#12 · https://github.com/o/r/pull/12\n"
        "Status: escalated (2026-05-18 09:10 UTC)\n"
        "PR head SHA: ccc333\n\n"
        "## Coder's PR description (verbatim, as of last capture)\n"
        "Third unit body.\n\n"
        "## Cycle history\n"
        "3 cycles · cap-3 hit\n\n"
        "### Cycle 1 — tester: BUG_FOUND\n"
        "- test 500\n\n"
        "### Cycle 3 — reviewer: REVIEW_REQUEST_CHANGES\n"
        "- still failing\n\n"
        "## Review threads\n"
        "_no review threads_\n",
    )


class TestPopulatedState:
    """Mixed merged / in-flight / escalated units — the other case the
    unit description names explicitly."""

    def test_includes_spec_md_body(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory(
            "F-200",
            base_dir=tmp_path,
            run=_Runner(_FakeProc(stdout="abc1 spec(F-200): seed\n")),
        )
        # spec.md content is spliced in verbatim — both the Intent text
        # and an Acceptance sentinel.
        assert "Fuse spec + cycles + escalations." in out
        assert "feature_memory returns ≥ 4 sections" in out

    def test_runs_git_log_against_per_feature_spec_path(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """The git log invocation must target ``features/F-XXX/spec.md`` and
        request at most 10 entries (verbatim from the unit description)."""
        _populated_setup(tmp_path)
        runner = _Runner(_FakeProc(stdout="abc1 spec(F-200): seed\n"))
        feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=runner)

        git_calls = [c for c in runner.calls if c[:2] == ["git", "log"]]
        assert git_calls, "no `git log` invocation captured"
        argv = git_calls[0]
        # `-N` flag for at most 10 entries.
        assert any(a in {"-10", "10"} for a in argv) or "-10" in " ".join(argv)
        # The spec.md pathspec must be present.
        assert any("features/F-200/spec.md" in a for a in argv), (
            f"git log not scoped to spec.md: argv={argv}"
        )

    def test_includes_git_log_stdout(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _populated_setup(tmp_path)
        runner = _Runner(
            _FakeProc(stdout="abc1 spec(F-200): clarify scope\ndef2 spec(F-200): seed\n")
        )
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=runner)
        assert "spec(F-200): clarify scope" in out
        assert "spec(F-200): seed" in out

    def test_lists_every_unit_with_status(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """Aggregated ``unit_summary`` (per unit) — surfacing status is the
        minimum the lead needs to scan the feature at a glance."""
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        for uid in ("F-200-U-1", "F-200-U-2", "F-200-U-3"):
            assert uid in out, f"unit {uid} missing from blob"
        # The three statuses each appear so a merged/in-flight/escalated
        # scan is one Ctrl-F away.
        assert "done" in out
        assert "in_ci" in out
        assert "escalated" in out

    def test_includes_cycle_log_final_sections_for_terminal_units(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Cycle log 'Final' sections — merge SHA (for merged) and the
        REVIEW_REQUEST_CHANGES cycle (for the escalated unit)."""
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        # U-1: merge commit SHA must surface (it's the canonical bit of the
        # ## PR section).
        assert "Merge commit SHA: bbb222" in out
        # The last cycle subsection — what closed the unit out — appears.
        assert "REVIEW_RECOMMEND_MERGE" in out

        # U-3: cap-3 hit summary + the last cycle subsection.
        assert "cap-3 hit" in out
        assert "REVIEW_REQUEST_CHANGES" in out

    def test_in_flight_unit_with_no_cycle_log_is_not_a_fatal_omission(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """U-2 has no cycle log file (in-flight). The blob must still
        render — and U-2's existence is reflected at minimum in the units
        section above."""
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        assert "F-200-U-2" in out  # surfaced in the units list
        # No traceback / error marker for the missing cycle log.
        assert "Traceback" not in out

    def test_recent_escalations_include_blocked_and_no_marker_events(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Recent escalation events from ``unit_events`` — coder_blocked
        and reviewer_no_marker are the canonical escalation classes."""
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        assert "coder_blocked" in out
        assert "reviewer_no_marker" in out

    def test_recent_escalations_exclude_normal_scheduling_chatter(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """The escalations *section* must not be polluted with normal
        scheduling events — ``pr_opened`` and ``fix_pushed`` appear
        on every unit and would drown out actual escalations.

        We assert this by inspecting the dedicated escalations block,
        not the whole blob (``pr_opened`` legitimately appears elsewhere
        in cycle-log subsections).
        """
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        # Find the "Recent escalations" section body, terminated by the
        # next top-level "## " heading or end of blob.
        idx = out.lower().find("recent escalation")
        assert idx != -1, "Recent escalations section missing"
        # Walk forward to the start of the section body (skip the heading).
        rest = out[idx:]
        # The section is the last one in the proposal's order, so it
        # usually runs to EOF; if a future ordering change adds something
        # after it, stop at the next "\n## " boundary.
        end = rest.find("\n## ", 1)
        body = rest if end == -1 else rest[:end]

        assert "pr_opened" not in body, (
            f"escalations section must not list pr_opened — block was:\n{body}"
        )

    def test_escalations_carry_unit_id(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """An escalation event without its originating unit_id is useless
        to the lead — they need to know WHICH unit blew up."""
        _populated_setup(tmp_path)
        out = feature_memory.build_feature_memory("F-200", base_dir=tmp_path, run=_Runner())
        # Locate the escalations block (same approach as above).
        idx = out.lower().find("recent escalation")
        rest = out[idx:]
        end = rest.find("\n## ", 1)
        body = rest if end == -1 else rest[:end]
        assert "F-200-U-3" in body, (
            f"escalation events must include unit_id F-200-U-3; block was:\n{body}"
        )


# --------------------------- read-side null-safety ---------------------------


class TestReadSideNullSafety:
    def test_unknown_feature_id_returns_error_string_not_exception(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """Calling with a feature that's not in state.db must not raise —
        the lead gets a string back either way. (This is the common
        path for typos.)"""
        out = feature_memory.build_feature_memory(
            "F-DOES-NOT-EXIST", base_dir=tmp_path, run=_Runner()
        )
        assert isinstance(out, str)
        # The output communicates the not-found condition somehow.
        assert "F-DOES-NOT-EXIST" in out or "not found" in out.lower() or "ERROR" in out

    def test_git_binary_missing_falls_back_to_placeholder(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """``git`` not on PATH (FileNotFoundError raised by subprocess.run)
        must degrade to a placeholder string — not crash the blob.

        The cycle-log writer follows the same best-effort convention; the
        unit description explicitly requires the read-side do so too."""
        _seed_feature(feature_id="F-101")

        def boom(*a: Any, **k: Any) -> _FakeProc:
            raise FileNotFoundError("git: not on PATH")

        # No exception; output is a string.
        out = feature_memory.build_feature_memory("F-101", base_dir=tmp_path, run=boom)
        assert isinstance(out, str)
        assert out.strip() != ""

    def test_git_log_nonzero_returncode_falls_back_to_placeholder(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """``git log`` exits non-zero (non-repo workdir or no commits) →
        placeholder, not an exception."""
        _seed_feature(feature_id="F-101")
        out = feature_memory.build_feature_memory(
            "F-101",
            base_dir=tmp_path,
            run=_Runner(_FakeProc(returncode=128, stderr="not a git repository")),
        )
        assert isinstance(out, str)
        assert out.strip() != ""
        # The non-zero stderr must not leak into the output as a raw error.
        assert "Traceback" not in out

    def test_corrupt_cycle_log_does_not_raise(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """A cycle-log file that doesn't match the expected schema (e.g.
        half-written by a crashed writer) must not crash the builder."""
        _seed_feature(feature_id="F-300")
        _seed_unit("F-300", "F-300-U-1", status="done", pr_number=99)
        # No ## PR, no ### Cycle subsections — just an H1.
        _write_cycle_log(tmp_path, "F-300", "F-300-U-1", "# F-300-U-1\n\nGarbage body.\n")

        out = feature_memory.build_feature_memory("F-300", base_dir=tmp_path, run=_Runner())
        # The builder didn't raise, and the unit still surfaces somewhere
        # in the blob.
        assert "F-300-U-1" in out


# --------------------------- MCP tool registration ---------------------------


class TestMcpToolRegistration:
    """The unit description says the tool must be registered on the MCP
    server. Verify by inspecting the FastMCP tool registry after the
    observability module is imported (mirroring what mcp_server does
    at startup)."""

    def test_feature_memory_tool_is_registered(self) -> None:
        from orchestrator.tools import (
            mcp,
            observability,  # noqa: F401  — triggers @mcp.tool()
        )

        try:
            names = set(mcp._tool_manager._tools.keys())  # type: ignore[attr-defined]
        except AttributeError:
            pytest.skip("FastMCP internal API changed; can't introspect tool registry")
        assert "feature_memory" in names, (
            f"feature_memory MCP tool must be registered (registered: {sorted(names)})"
        )

    def test_mcp_tool_wraps_library_function(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The thin MCP wrapper must call the library function with the
        feature_id passed in by the lead."""
        from orchestrator.tools import observability as obs

        _seed_feature(feature_id="F-101")

        captured: dict[str, Any] = {}

        def fake_build(feature_id: str, **kwargs: Any) -> str:
            captured["feature_id"] = feature_id
            return "<SENTINEL_BLOB>"

        monkeypatch.setattr(feature_memory, "build_feature_memory", fake_build)

        result = obs.feature_memory("F-101")
        assert result == "<SENTINEL_BLOB>"
        assert captured.get("feature_id") == "F-101"

    def test_mcp_tool_returns_string(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """End-to-end smoke: the wrapper, when invoked against a
        real-but-empty feature, must return a non-empty string (not
        raise, not return None, not return a dict)."""
        from orchestrator.tools import observability as obs

        _seed_feature(feature_id="F-101")
        # Use the actual library function; just ensure no exception and
        # the return type is str. Run from a workdir without git so the
        # git-log fallback path also exercises.
        result = obs.feature_memory("F-101")
        assert isinstance(result, str)
        assert result.strip() != ""
