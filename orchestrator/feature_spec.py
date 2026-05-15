"""Per-feature spec.md writer.

Phase 1 of the feature-spec + cycle-logs work (see
`docs/PROPOSAL-feature-spec-and-headless-daemon.md` §1 and `docs/SPEC-FORMAT.md`).

The orchestrator owns a `features/<feature_id>/` directory rooted next to
`state.db`. On `load_feature`, this module seeds `features/<feature_id>/spec.md`
with a starter template carrying the feature's title and description. The
lead and later units edit the file; this module never overwrites a spec that
already exists on disk, so manual edits are preserved across re-invocations
of `load_feature`.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator import state

SPEC_FILENAME = "spec.md"


def features_root() -> Path:
    """Return the `features/` directory next to `state.db`.

    Derived from `state.STATE_DB` rather than `__file__` so tests using the
    `tmp_state_db` fixture (which monkeypatches `STATE_DB` to a temp path)
    write into the same tmp tree, not into the real repo.
    """
    return Path(state.STATE_DB).parent / "features"


def spec_path(feature_id: str) -> Path:
    """Return the on-disk path to a feature's `spec.md`."""
    return features_root() / feature_id / SPEC_FILENAME


def render_template(feature_id: str, title: str, description: str) -> str:
    """Render the starter spec.md body for a freshly-loaded feature.

    Intent is pre-filled from the feature description. Every other section
    is left as a `_TBD_` placeholder so the lead can spot what still needs
    filling in during planning.
    """
    return (
        f"# {feature_id}: {title}\n"
        "\n"
        "## Intent\n"
        f"{description}\n"
        "\n"
        "## Acceptance\n"
        '_TBD — concrete, testable criteria for "done"._\n'
        "\n"
        "## Out of scope\n"
        "_TBD — hard boundary against scope creep._\n"
        "\n"
        "## Approach\n"
        "_TBD — high-level design choices, library / framework decisions._\n"
        "\n"
        "## Constraints\n"
        "_TBD — non-functional requirements (perf, security, compatibility)._\n"
        "\n"
        "## Decisions\n"
        "_None yet. Non-obvious choices land here as planning and execution "
        "progress; the commit message for each edit carries the `Why:` "
        "line (see `docs/SPEC-FORMAT.md`)._\n"
        "\n"
        "## Open questions\n"
        "_None yet. Resolved questions move to Decisions._\n"
    )


def write_spec_if_missing(feature_id: str, title: str, description: str) -> Path:
    """Create `features/<feature_id>/spec.md` from the template if absent.

    Preserves existing files untouched — the lead edits spec.md during
    planning, and re-calling `load_feature` (e.g. to fix a wrong
    `repo_path`) must not clobber those edits. Uses write-to-tmp + rename
    for partial-write protection, matching the persistence rule in
    `docs/PROPOSAL-feature-spec-and-headless-daemon.md` §2.

    Returns the path regardless of whether a new file was written; callers
    can `stat` it if they need the "was this a first write?" signal.
    """
    path = spec_path(feature_id)
    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render_template(feature_id, title, description), encoding="utf-8")
    tmp.replace(path)
    return path
