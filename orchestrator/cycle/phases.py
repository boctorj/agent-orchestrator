"""Per-phase advance helpers shared by the blocking caller + the daemon.

F-016 Phase 5 (spec § "Phase 5 — Cleanup": *"The blocking helpers
(`_wait_ci_with_fix_loop`, `_tester_phase`, `_reviewer_phase`,
`_ultrareview_phase`) move to a new `orchestrator/cycle/phases.py`
module where both the daemon and the explicit `cycle_review_blocking`
call them."*).

The helpers themselves still live in :mod:`orchestrator.tools.execution`
— that module owns ``CycleContext``, the per-cycle audit-log writers,
and the role-resume / spawn helpers the phases compose. This module is
the public-API surface those helpers ship under: both callers
(``cycle_review_blocking`` today, the daemon's future drive path) import
the names from here, so a later move of the implementation into this
file is invisible to callers.

Importing this module is cheap — it triggers nothing beyond
:mod:`orchestrator.tools.execution`'s normal load.
"""

from __future__ import annotations

# Re-exports — the daemon and ``cycle_review_blocking`` reach for the
# same names through this module so the "one engine, two callers"
# contract has a single import location. The leading-underscore names
# are intentionally preserved because they remain implementation
# details of the cycle pipeline; "shared by daemon + blocking caller"
# is not the same as "public API for the chat persona".
from orchestrator.tools.execution import (
    CycleContext,
    _conflict_fix_loop,
    _copilot_phase,
    _emit_terminal,
    _reviewer_phase,
    _run_reviewer_advance,
    _run_terminal_advance,
    _run_tester_advance,
    _tester_phase,
    _ultrareview_phase,
    _wait_ci_with_fix_loop,
)

__all__ = (
    "CycleContext",
    "_conflict_fix_loop",
    "_copilot_phase",
    "_emit_terminal",
    "_reviewer_phase",
    "_run_reviewer_advance",
    "_run_terminal_advance",
    "_run_tester_advance",
    "_tester_phase",
    "_ultrareview_phase",
    "_wait_ci_with_fix_loop",
)
