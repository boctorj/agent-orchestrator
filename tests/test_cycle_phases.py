"""F-016-U-7 — orchestrator/cycle/phases.py shared import surface."""

from __future__ import annotations

import inspect

from orchestrator.cycle import phases
from orchestrator.tools import execution


def test_phases_module_re_exports_blocking_helpers():
    """Spec § Phase 5: "The blocking helpers (_wait_ci_with_fix_loop,
    _tester_phase, _reviewer_phase, _ultrareview_phase) move to a new
    orchestrator/cycle/phases.py module where both the daemon and the
    explicit cycle_review_blocking call them."

    The U-7 implementation creates ``orchestrator/cycle/phases.py`` as
    the shared import location (the helpers stay in
    ``orchestrator.tools.execution`` for now to keep U-7's diff
    minimal; the public-API surface moves now so future callers reach
    for the stable module). Pin every name the spec lists."""
    required = {
        "_wait_ci_with_fix_loop",
        "_tester_phase",
        "_reviewer_phase",
        "_ultrareview_phase",
    }
    missing = required - set(phases.__all__)
    assert not missing, f"phases module missing required exports: {missing}"


def test_phases_module_exposes_run_advance_helpers():
    """The Phase 4 (U-6) ``_run_tester_advance`` / ``_run_reviewer_advance``
    / ``_run_terminal_advance`` shared engine moves with the rest —
    spec § Constraints 3 ("No parallel state machine ... call the *same*
    derive_next_action + execute engine") makes them part of the
    shared surface."""
    for name in ("_run_tester_advance", "_run_reviewer_advance", "_run_terminal_advance"):
        assert name in phases.__all__
        assert hasattr(phases, name)


def test_phases_helpers_identical_to_execution_module():
    """The re-exports must point at the live functions in
    ``orchestrator.tools.execution`` — not a frozen copy. A test that
    ``monkeypatch.setattr(execution, "_tester_phase", ...)`` to swap
    behaviour must affect the shared module too, else daemon and
    blocking caller would silently see different implementations."""
    for name in phases.__all__:
        assert getattr(phases, name) is getattr(execution, name), (
            f"phases.{name} diverged from execution.{name} — the U-7 "
            "shared-import-surface contract is broken; both callers must "
            "see the same engine."
        )


def test_phases_module_is_signature_stable():
    """The shared module's __all__ is documented; new exports must be
    explicit. This test pins the current surface so a future drive-by
    addition needs a deliberate update to __all__ + this test."""
    assert phases.__all__ == (
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


def test_phases_helpers_have_real_signatures():
    """Sanity check that the imports landed live functions, not None /
    sentinels. ``inspect.signature`` raising would mean a re-export
    typo went unnoticed."""
    for name in phases.__all__:
        fn = getattr(phases, name)
        # ``CycleContext`` is a dataclass; the rest are callables.
        if name == "CycleContext":
            assert hasattr(fn, "__dataclass_fields__")
            continue
        assert callable(fn)
        # signature() raises on builtin/no-source — every helper here
        # is Python source so it must succeed.
        inspect.signature(fn)
