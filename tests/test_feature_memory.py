"""Tests for orchestrator/feature_memory.py — session-bootstrap blob.

The library function ``build_feature_memory(feature_id)`` returns a
~3-7K-token markdown string the lead reads at session start. It splices
together:

  * the feature's ``spec.md`` (or a placeholder if absent),
  * ``git log -10 -- features/F-XXX/spec.md`` (or a placeholder if not
    a git repo / no commits),
  * an aggregated ``unit_summary`` per unit known to ``state.work_units``,
  * cycle-log "Final" sections (PR + last cycle subsection) for every
    unit whose cycle log file exists on disk,
  * recent escalation events across the feature.

Tests cover the two cases the unit description names explicitly:

  1. Empty state — feature exists but no spec.md, no units, no cycle
     logs, no escalation events. The blob still renders with placeholders.
  2. Populated state — mix of merged / in-flight / escalated units, a
     real spec.md, cycle logs on disk, escalation events in the audit log.

Plus a third case for the unit description's read-side null-safety
clause: "Read-side must handle missing features/ directory gracefully."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from orchestrator import feature_memory, state
from orchestrator.models import Feature, WorkUnitState

# --------------------------- fakes ---------------------------


@dataclass
class FakeProc:
    """Minimal ``subprocess.CompletedProcess`` look-alike for the git log call."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeRunner:
    """Records argv and returns canned responses keyed by command prefix.

    Same shape as ``FakeRunner`` in ``tests/test_cycle_log.py``; kept local
    rather than imported because the test files exercise different
    subprocess surfaces (``gh`` vs. ``git log``).
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], FakeProc]] = []

    def register(self, prefix: tuple[str, ...], proc: FakeProc) -> None:
        self._responses.append((prefix, proc))

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append(list(argv))
        for prefix, proc in self._responses:
            if tuple(argv[: len(prefix)]) == prefix:
                return proc
        return FakeProc()


# --------------------------- helpers ---------------------------


def _seed_feature(
    feature_id: str = "F-007",
    title: str = "OAuth",
    description: str = "Add Google OAuth login.",
    repo_path: str = "https://github.com/o/r",
) -> Feature:
    feature = Feature(
        id=feature_id,
        title=title,
        description=description,
        repo_path=repo_path,
    )
    state.save_feature(feature)
    return feature


def _seed_unit(
    feature_id: str,
    unit_id: str,
    *,
    status: str = "pending",
    pr_number: int | None = None,
    title: str = "u",
    last_error: str = "",
    events: list[dict[str, Any]] | None = None,
) -> None:
    state.upsert_unit_state(
        WorkUnitState(
            unit_id=unit_id,
            feature_id=feature_id,
            status=status,
            pr_number=pr_number,
            last_error=last_error,
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
    target_dir = base_dir / "features" / feature_id
    target_dir.mkdir(parents=True, exist_ok=True)
    # Use the same naming as cycle_log.cycle_log_path: U-N.md.
    tail = unit_id.rsplit("-", 2)[-1]
    target = target_dir / f"U-{tail}.md"
    target.write_text(body, encoding="utf-8")
    return target


# --------------------------- empty-state case ---------------------------


class TestEmptyState:
    """Feature exists in state but nothing else does — no spec.md, no
    cycle logs, no units, no events. The blob renders with placeholders
    rather than crashing or returning a bare ERROR."""

    def test_renders_all_sections_with_placeholders(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_feature(feature_id="F-099", title="Empty", description="nothing yet")
        runner = FakeRunner()
        # `features/` doesn't exist at all → git log call would normally
        # produce nothing; mirror that with a non-zero return.
        runner.register(("git", "log"), FakeProc(returncode=128, stderr="not a repo"))

        out = feature_memory.build_feature_memory("F-099", base_dir=tmp_path, run=runner)

        # All section headers present.
        for header in (
            "# Feature memory: F-099",
            "## spec.md",
            "## Recent spec.md edits",
            "## Units",
            "## Cycle log finals",
            "## Recent escalations",
        ):
            assert header in out, f"missing section: {header}"

    def test_handles_missing_features_directory(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """Read-side must handle missing features/ directory gracefully —
        explicit constraint in the unit description."""
        _seed_feature(feature_id="F-099", title="t", description="d")
        assert not (tmp_path / "features").exists()

        # No exception — the function must succeed even when nothing is on disk.
        out = feature_memory.build_feature_memory("F-099", base_dir=tmp_path, run=FakeRunner())

        assert "F-099" in out
        # spec.md placeholder present rather than file contents.
        spec_block = _section(out, "## spec.md")
        assert "no spec.md" in spec_block.lower() or "_no spec.md_" in spec_block

    def test_no_units_shows_placeholder(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed_feature(feature_id="F-099", title="t", description="d")
        out = feature_memory.build_feature_memory("F-099", base_dir=tmp_path, run=FakeRunner())
        units_block = _section(out, "## Units")
        assert "no units" in units_block.lower()

    def test_no_cycle_logs_shows_placeholder(self, tmp_path: Path, tmp_state_db: Path) -> None:
        _seed_feature(feature_id="F-099", title="t", description="d")
        out = feature_memory.build_feature_memory("F-099", base_dir=tmp_path, run=FakeRunner())
        cycle_block = _section(out, "## Cycle log finals")
        assert "no cycle log" in cycle_block.lower()

    def test_no_escalation_events_shows_placeholder(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        _seed_feature(feature_id="F-099", title="t", description="d")
        out = feature_memory.build_feature_memory("F-099", base_dir=tmp_path, run=FakeRunner())
        esc_block = _section(out, "## Recent escalations")
        assert "no escalation" in esc_block.lower()


# --------------------------- populated case ---------------------------


class TestPopulatedState:
    """Mix of merged / in-flight / escalated units, a real spec.md, cycle
    logs on disk for the merged units, and a stream of escalation events."""

    def _setup(self, tmp_path: Path) -> None:
        _seed_feature(feature_id="F-007", title="OAuth", description="Add Google OAuth login.")

        # spec.md on disk
        spec_dir = tmp_path / "features" / "F-007"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "# F-007: OAuth\n\n## Intent\nAdd Google OAuth login.\n\n"
            "## Acceptance\nOAuth flow round-trips.\n",
            encoding="utf-8",
        )

        # U-1: merged (status=done) with a cycle log on disk
        _seed_unit(
            "F-007",
            "F-007-U-1",
            status="done",
            pr_number=10,
            title="OAuth flow",
            events=[
                {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #10"},
                {
                    "event_type": "reviewer_recommend_merge",
                    "cycle_number": 1,
                    "summary": "clean PR — endorse",
                },
            ],
        )
        _write_cycle_log(
            tmp_path,
            "F-007",
            "F-007-U-1",
            "# F-007-U-1 — OAuth flow\n\n"
            "## PR\n"
            "#10 · https://github.com/o/r/pull/10\n"
            "Status: done (2026-05-17 02:48 UTC)\n"
            "PR head SHA: abc123\n"
            "Merge commit SHA: def456\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "Adds OAuth handler.\n\n"
            "## Cycle history\n"
            "1 cycles · cap-3 not hit\n\n"
            "### Cycle 0 — coder: PR opened\n"
            "- PR #10\n\n"
            "### Cycle 1 — reviewer: REVIEW_RECOMMEND_MERGE\n"
            "- clean PR — endorse\n\n"
            "## Review threads\n"
            "_no review threads_\n",
        )

        # U-2: in-flight (status=in_ci), no cycle log yet
        _seed_unit(
            "F-007",
            "F-007-U-2",
            status="in_ci",
            pr_number=11,
            title="OAuth callback",
            events=[
                {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #11"},
                {"event_type": "fix_pushed", "cycle_number": 1, "summary": "fix"},
            ],
        )

        # U-3: escalated (cap-3 hit), with a cycle log on disk plus
        # a string of escalation events.
        _seed_unit(
            "F-007",
            "F-007-U-3",
            status="escalated",
            pr_number=12,
            title="OAuth token refresh",
            last_error="BLOCKED [auth_failure]: 401 from gh api",
            events=[
                {"event_type": "pr_opened", "cycle_number": 0, "summary": "PR #12"},
                {
                    "event_type": "tester_bug_found",
                    "cycle_number": 1,
                    "summary": "refresh 500",
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
                    "summary": "No marker emitted",
                },
            ],
        )
        _write_cycle_log(
            tmp_path,
            "F-007",
            "F-007-U-3",
            "# F-007-U-3 — OAuth token refresh\n\n"
            "## PR\n"
            "#12 · https://github.com/o/r/pull/12\n"
            "Status: escalated (2026-05-18 09:10 UTC)\n"
            "PR head SHA: ffff111\n\n"
            "## Coder's PR description (verbatim, as of last capture)\n"
            "Adds refresh path.\n\n"
            "## Cycle history\n"
            "3 cycles · cap-3 hit\n\n"
            "### Cycle 1 — tester: BUG_FOUND\n"
            "- refresh 500\n\n"
            "### Cycle 3 — reviewer: REVIEW_REQUEST_CHANGES\n"
            "- 🔴 token storage skipped wrap\n\n"
            "## Review threads\n"
            "_no review threads_\n",
        )

    def test_includes_spec_md_contents(self, tmp_path: Path, tmp_state_db: Path) -> None:
        self._setup(tmp_path)
        runner = FakeRunner()
        runner.register(
            ("git", "log"),
            FakeProc(
                stdout="abc1234 spec(F-007): clarify Fernet scope\ndef5678 spec(F-007): initial seed\n"
            ),
        )

        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=runner)

        spec_block = _section(out, "## spec.md")
        assert "## Intent" in spec_block
        assert "Add Google OAuth login." in spec_block
        assert "OAuth flow round-trips." in spec_block

    def test_includes_git_log_output(self, tmp_path: Path, tmp_state_db: Path) -> None:
        self._setup(tmp_path)
        runner = FakeRunner()
        runner.register(
            ("git", "log"),
            FakeProc(
                stdout=(
                    "abc1234 spec(F-007): clarify Fernet scope\ndef5678 spec(F-007): initial seed\n"
                )
            ),
        )

        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=runner)

        log_block = _section(out, "## Recent spec.md edits")
        assert "spec(F-007): clarify Fernet scope" in log_block
        assert "spec(F-007): initial seed" in log_block

        # The git log call must be scoped to the per-feature spec path.
        git_call = next(c for c in runner.calls if c[:2] == ["git", "log"])
        assert "features/F-007/spec.md" in git_call
        # And limited to ~10 entries per the unit description.
        assert "-10" in git_call

    def test_lists_every_unit_with_status(self, tmp_path: Path, tmp_state_db: Path) -> None:
        self._setup(tmp_path)
        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())

        units_block = _section(out, "## Units")
        for uid in ("F-007-U-1", "F-007-U-2", "F-007-U-3"):
            assert uid in units_block, f"unit {uid} missing from summary"
        # Status labels surface so the lead can scan merged / in-flight / escalated at a glance.
        assert "done" in units_block
        assert "in_ci" in units_block
        assert "escalated" in units_block

    def test_cycle_log_finals_include_pr_and_last_cycle(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        self._setup(tmp_path)
        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())

        cycle_block = _section(out, "## Cycle log finals")

        # U-1 (merged): PR section + the REVIEW_RECOMMEND_MERGE cycle.
        assert "F-007-U-1" in cycle_block
        assert "Merge commit SHA: def456" in cycle_block
        assert "REVIEW_RECOMMEND_MERGE" in cycle_block

        # U-3 (escalated): cap-3 hit line + the request-changes cycle.
        assert "F-007-U-3" in cycle_block
        assert "3 cycles · cap-3 hit" in cycle_block
        assert "REVIEW_REQUEST_CHANGES" in cycle_block

    def test_cycle_log_finals_omit_units_without_on_disk_log(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """U-2 is in-flight; its cycle log isn't written until terminal
        state. The finals section silently skips it rather than emitting
        a stub — the units list (above) already calls out its status."""
        self._setup(tmp_path)
        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())

        cycle_block = _section(out, "## Cycle log finals")
        # The U-2 unit_id doesn't appear in cycle_block — but it appears in
        # the units list above, which is the right place for it.
        assert "F-007-U-2" not in cycle_block, (
            "in-flight unit must not appear in cycle log finals — its log isn't written yet"
        )

    def test_recent_escalations_lists_blocked_no_marker_error_events(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        self._setup(tmp_path)
        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())

        esc_block = _section(out, "## Recent escalations")
        # Only escalation-class events surface — not pr_opened, not fix_pushed.
        assert "coder_blocked" in esc_block
        assert "reviewer_no_marker" in esc_block
        # pr_opened is normal scheduling chatter; it must NOT appear here.
        assert "pr_opened" not in esc_block
        assert "fix_pushed" not in esc_block

    def test_recent_escalations_carry_unit_id_for_traceability(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """The lead needs to know which unit blew up — the events must be
        tagged with the originating unit_id, not collapsed into a list of
        free-floating event types."""
        self._setup(tmp_path)
        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())

        esc_block = _section(out, "## Recent escalations")
        assert "F-007-U-3" in esc_block, "escalation events must surface their unit_id"


# --------------------------- additional read-side null-safety ---------------------------


class TestReadSideNullSafety:
    def test_feature_not_found_returns_error_string(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        out = feature_memory.build_feature_memory("F-404", base_dir=tmp_path, run=FakeRunner())
        assert out.startswith("ERROR")
        assert "F-404" in out

    def test_git_missing_falls_back_to_placeholder(
        self, tmp_path: Path, tmp_state_db: Path
    ) -> None:
        """A missing `git` binary or a non-repo workdir shouldn't block —
        the writer convention from cycle_log.py is "best-effort, never
        raise"."""
        _seed_feature(feature_id="F-007")

        def boom(*a: Any, **k: Any) -> None:
            raise FileNotFoundError("git")

        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=boom)
        log_block = _section(out, "## Recent spec.md edits")
        # Some recognizable placeholder must appear — exact wording is
        # implementation detail.
        assert log_block.strip() != ""
        # Must NOT raise.

    def test_corrupt_cycle_log_does_not_raise(self, tmp_path: Path, tmp_state_db: Path) -> None:
        """A cycle log file that doesn't match the expected schema (e.g.
        a half-written log from a crashed writer) must not crash the
        builder; the section just shows whatever it can extract."""
        _seed_feature(feature_id="F-007")
        _seed_unit("F-007", "F-007-U-1", status="done", pr_number=10)
        # No '## PR' heading, no '### Cycle' subsections — just an H1.
        _write_cycle_log(tmp_path, "F-007", "F-007-U-1", "# F-007-U-1\n\nGarbage.\n")

        out = feature_memory.build_feature_memory("F-007", base_dir=tmp_path, run=FakeRunner())
        # No crash, and the unit header still appears.
        assert "F-007-U-1" in out


# --------------------------- MCP-tool wrapper ---------------------------


class TestMcpTool:
    """The MCP tool wrapper in tools.observability must be registered and
    return the same blob that the library function returns."""

    def test_tool_is_registered(self) -> None:
        # Import the tools module so its @mcp.tool() decorators fire; this
        # mirrors what `orchestrator.mcp_server` does at startup.
        from orchestrator.tools import mcp, observability  # noqa: F401

        try:
            registered = set(mcp._tool_manager._tools.keys())  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover — defensive against FastMCP API change
            registered = {name for name in dir(observability) if not name.startswith("_")}

        assert "feature_memory" in registered, (
            "feature_memory MCP tool must be registered (F-006-U-5 acceptance)"
        )

    def test_tool_returns_library_output(
        self, tmp_path: Path, tmp_state_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from orchestrator.tools import observability as obs

        _seed_feature(feature_id="F-099", title="t", description="d")

        captured = {}

        def fake_build(feature_id: str, **kwargs: Any) -> str:
            captured["feature_id"] = feature_id
            return "FAKE_MEMORY_BLOB"

        monkeypatch.setattr(feature_memory, "build_feature_memory", fake_build)

        result = obs.feature_memory("F-099")
        assert result == "FAKE_MEMORY_BLOB"
        assert captured["feature_id"] == "F-099"


# --------------------------- helpers used above ---------------------------


# Known top-level section headers in the feature_memory blob. The test
# helper uses this list to detect the boundary between sections so that
# embedded `##` / `###` headings inside spec.md or cycle logs don't
# falsely terminate a section.
_KNOWN_SECTIONS = (
    "## spec.md",
    "## Recent spec.md edits",
    "## Units",
    "## Cycle log finals",
    "## Recent escalations",
)


def _section(blob: str, header: str) -> str:
    """Return the body of one top-level `## Heading` section in ``blob``.

    Walks until the next known outer section heading rather than the
    next bare `## ` — embedded spec.md / cycle-log content contains its
    own `##` headings (`## Intent`, `## PR`, ...) that must NOT terminate
    the outer section.
    """
    lines = blob.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith(header):
            in_section = True
            continue
        if in_section and any(line.startswith(s) for s in _KNOWN_SECTIONS if s != header):
            break
        if in_section:
            out.append(line)
    return "\n".join(out)
