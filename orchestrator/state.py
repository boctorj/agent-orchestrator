"""SQLite state layer for the orchestrator.

Single-file DB at the project root. All MCP tools read/write through here.
Work units are stored as JSON inside the plans table for now; Stage 3 may
split to a dedicated units table for per-unit status tracking.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.models import (
    Feature,
    Plan,
    VerificationResult,
    VerifiedRepo,
    WorkUnit,
    WorkUnitState,
)

STATE_DB = Path(__file__).parent.parent / "state.db"

# TTL for cached verified_repos rows. After this, the orchestrator
# re-verifies on next access. 24h balances "branch protection rarely
# changes" against "we still notice if someone disabled it yesterday."
VERIFY_TTL_HOURS = 24


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection, then commit/rollback AND close on exit.

    `with sqlite3.Connection as conn:` only commits/rollbacks the active
    transaction — it leaves the underlying file descriptor open until GC.
    Under pytest that triggers thousands of ResourceWarnings, and under
    real load it leaks fds. Wrapping with `@contextmanager` lets every
    `with _connect() as conn:` callsite stay unchanged while guaranteeing
    `conn.close()` runs on both happy-path and exception exits.
    """
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:  # commits on success, rolls back if an exception escapes
            yield conn
    finally:
        conn.close()


def _migrate_features_ultrareview(conn: sqlite3.Connection) -> None:
    """Add `ultrareview_enabled` to `features` for DBs created pre-F-007.

    `CREATE TABLE IF NOT EXISTS` is a no-op when the table already exists,
    so new columns won't appear in pre-existing state.db files without an
    explicit ALTER. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we probe
    `PRAGMA table_info` first to keep the migration idempotent.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(features)").fetchall()}
    if "ultrareview_enabled" not in cols:
        conn.execute(
            "ALTER TABLE features ADD COLUMN ultrareview_enabled INTEGER NOT NULL DEFAULT 0"
        )


def init_db() -> None:
    """Create tables if they don't exist, then apply column migrations.

    Idempotent — safe to call repeatedly. The migration step is what
    upgrades existing state.db files when new columns are added to
    already-shipped tables (CREATE TABLE IF NOT EXISTS doesn't touch
    existing schemas).
    """
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS features (
                id                  TEXT PRIMARY KEY,
                title               TEXT NOT NULL,
                description         TEXT NOT NULL,
                repo_path           TEXT NOT NULL DEFAULT '',
                branch_prefix       TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'draft',
                created_at          TEXT NOT NULL,
                ultrareview_enabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS plans (
                feature_id   TEXT PRIMARY KEY,
                units_json   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'draft',
                approved_at  TEXT,
                FOREIGN KEY (feature_id) REFERENCES features(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS work_units (
                unit_id              TEXT PRIMARY KEY,
                feature_id           TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'pending',
                branch               TEXT NOT NULL DEFAULT '',
                pr_number            INTEGER,
                coder_session_id     TEXT NOT NULL DEFAULT '',
                tester_session_id    TEXT NOT NULL DEFAULT '',
                reviewer_session_id  TEXT NOT NULL DEFAULT '',
                review_round         INTEGER NOT NULL DEFAULT 0,
                last_activity        TEXT NOT NULL DEFAULT '',
                last_error           TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (feature_id) REFERENCES features(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS unit_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id       TEXT NOT NULL,
                feature_id    TEXT NOT NULL,
                ts            TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                source        TEXT NOT NULL DEFAULT 'orchestrator',
                cycle_number  INTEGER,
                summary       TEXT NOT NULL DEFAULT '',
                details       TEXT NOT NULL DEFAULT '',
                session_id    TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (unit_id) REFERENCES work_units(unit_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_unit_events_unit_ts
                ON unit_events(unit_id, ts);

            CREATE TABLE IF NOT EXISTS cached_resources (
                role            TEXT NOT NULL,
                prompt_hash     TEXT NOT NULL,
                agent_id        TEXT NOT NULL,
                environment_id  TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (role, prompt_hash)
            );

            CREATE TABLE IF NOT EXISTS verified_repos (
                repo_url                TEXT PRIMARY KEY,
                default_branch          TEXT NOT NULL,
                auth_mode               TEXT NOT NULL,
                auth_identity           TEXT NOT NULL DEFAULT '',
                verified_at             TEXT NOT NULL,
                has_branch_protection   INTEGER NOT NULL DEFAULT 0,
                required_approvals      INTEGER NOT NULL DEFAULT 0,
                blocks_force_push       INTEGER NOT NULL DEFAULT 0,
                blocks_deletion         INTEGER NOT NULL DEFAULT 0,
                blocks_bypass           INTEGER NOT NULL DEFAULT 0,
                has_codeowners          INTEGER NOT NULL DEFAULT 0,
                requires_signed_commits INTEGER NOT NULL DEFAULT 0,
                warnings_json           TEXT NOT NULL DEFAULT '[]'
            );
            """
        )
        _migrate_features_ultrareview(conn)


# --------------------------- features ---------------------------


def save_feature(feature: Feature) -> None:
    if not feature.created_at:
        feature.created_at = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO features (
                id, title, description, repo_path, branch_prefix, status,
                created_at, ultrareview_enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                repo_path=excluded.repo_path,
                branch_prefix=excluded.branch_prefix,
                status=excluded.status,
                ultrareview_enabled=excluded.ultrareview_enabled
            """,
            (
                feature.id,
                feature.title,
                feature.description,
                feature.repo_path,
                feature.branch_prefix,
                feature.status,
                feature.created_at,
                1 if feature.ultrareview_enabled else 0,
            ),
        )


def _feature_from_row(row: sqlite3.Row) -> Feature:
    """Build a `Feature` from a `features` row, coercing int→bool flags.

    SQLite stores `ultrareview_enabled` as INTEGER (0/1); the dataclass
    declares it as `bool`. `Feature(**dict(row))` would propagate the int,
    breaking `is True`/`is False` checks. Centralize the coercion here so
    add-a-column edits only touch one place (mirrors `_verified_repo_from_row`).
    """
    return Feature(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        repo_path=row["repo_path"],
        branch_prefix=row["branch_prefix"],
        status=row["status"],
        created_at=row["created_at"],
        ultrareview_enabled=bool(row["ultrareview_enabled"]),
    )


def get_feature(feature_id: str) -> Feature | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM features WHERE id = ?", (feature_id,)).fetchone()
    return _feature_from_row(row) if row else None


def list_features() -> list[Feature]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM features ORDER BY created_at").fetchall()
    return [_feature_from_row(r) for r in rows]


def next_feature_id() -> str:
    """Auto-allocate the next F-NNN id based on existing features."""
    import contextlib

    existing = list_features()
    nums = []
    for f in existing:
        if f.id.startswith("F-"):
            with contextlib.suppress(ValueError, IndexError):
                nums.append(int(f.id.split("-")[1]))
    next_num = (max(nums) + 1) if nums else 1
    return f"F-{next_num:03d}"


# --------------------------- plans ---------------------------


def save_plan(feature_id: str, units: list[WorkUnit]) -> None:
    units_json = json.dumps([asdict(u) for u in units])
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO plans (feature_id, units_json, status)
            VALUES (?, ?, 'draft')
            ON CONFLICT(feature_id) DO UPDATE SET
                units_json=excluded.units_json,
                status='draft',
                approved_at=NULL
            """,
            (feature_id, units_json),
        )
        # also bump the feature status
        conn.execute(
            "UPDATE features SET status='planned' WHERE id = ? AND status = 'draft'",
            (feature_id,),
        )


def get_plan(feature_id: str) -> Plan | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM plans WHERE feature_id = ?", (feature_id,)).fetchone()
    if not row:
        return None
    units = [WorkUnit(**u) for u in json.loads(row["units_json"])]
    return Plan(
        feature_id=row["feature_id"],
        units=units,
        status=row["status"],
        approved_at=row["approved_at"],
    )


def approve_plan(feature_id: str) -> str:
    """Mark plan approved. Returns the timestamp."""
    ts = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE plans SET status='approved', approved_at=? WHERE feature_id=?",
            (ts, feature_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No plan exists for {feature_id} — call save_plan first")
        conn.execute(
            "UPDATE features SET status='approved' WHERE id = ?",
            (feature_id,),
        )
    return ts


# --------------------------- work units ---------------------------


def upsert_unit_state(unit: WorkUnitState) -> None:
    if not unit.last_activity:
        unit.last_activity = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO work_units (
                unit_id, feature_id, status, branch, pr_number,
                coder_session_id, tester_session_id, reviewer_session_id,
                review_round, last_activity, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id) DO UPDATE SET
                status=excluded.status,
                branch=excluded.branch,
                pr_number=excluded.pr_number,
                coder_session_id=excluded.coder_session_id,
                tester_session_id=excluded.tester_session_id,
                reviewer_session_id=excluded.reviewer_session_id,
                review_round=excluded.review_round,
                last_activity=excluded.last_activity,
                last_error=excluded.last_error
            """,
            (
                unit.unit_id,
                unit.feature_id,
                unit.status,
                unit.branch,
                unit.pr_number,
                unit.coder_session_id,
                unit.tester_session_id,
                unit.reviewer_session_id,
                unit.review_round,
                unit.last_activity,
                unit.last_error,
            ),
        )


def get_unit_state(unit_id: str) -> WorkUnitState | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM work_units WHERE unit_id = ?", (unit_id,)).fetchone()
    return WorkUnitState(**dict(row)) if row else None


def list_unit_states(feature_id: str) -> list[WorkUnitState]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM work_units WHERE feature_id = ? ORDER BY unit_id",
            (feature_id,),
        ).fetchall()
    return [WorkUnitState(**dict(r)) for r in rows]


def touch_unit(unit_id: str, *, status: str | None = None, error: str = "") -> None:
    """Lightweight status update — bumps last_activity, optionally sets status/error."""
    with _connect() as conn:
        updates = ["last_activity = ?"]
        params: list = [_now()]
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if error:
            updates.append("last_error = ?")
            params.append(error)
        params.append(unit_id)
        # `updates` is built from literal column-name strings in this function;
        # all dynamic values bind via the params list — not user input.
        conn.execute(
            f"UPDATE work_units SET {', '.join(updates)} WHERE unit_id = ?",  # noqa: S608  # nosec B608
            params,
        )


def increment_review_round(unit_id: str) -> int:
    """Bump the review_round counter on a unit. Returns the new value."""
    with _connect() as conn:
        conn.execute(
            "UPDATE work_units SET review_round = review_round + 1, last_activity = ? WHERE unit_id = ?",
            (_now(), unit_id),
        )
        row = conn.execute(
            "SELECT review_round FROM work_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
    return row["review_round"] if row else 0


# --------------------------- unit events (audit log) ---------------------------


def record_event(
    unit_id: str,
    feature_id: str,
    event_type: str,
    *,
    source: str = "orchestrator",
    cycle_number: int | None = None,
    summary: str = "",
    details: str = "",
    session_id: str = "",
) -> None:
    """Append a row to unit_events. Never overwrites."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO unit_events
                (unit_id, feature_id, ts, event_type, source, cycle_number, summary, details, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id,
                feature_id,
                _now(),
                event_type,
                source,
                cycle_number,
                summary,
                details,
                session_id,
            ),
        )


def list_events(unit_id: str, limit: int = 200) -> list[dict]:
    """Return events for a unit, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM unit_events WHERE unit_id = ? ORDER BY ts LIMIT ?",
            (unit_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def summarize_unit(unit_id: str) -> dict:
    """Build a human-friendly digest of a unit's lifecycle.

    Returns a dict with: unit_state, cycle_count, event_counts_by_type,
    last_event, first_event, current_cycle.
    """
    state = get_unit_state(unit_id)
    events = list_events(unit_id)
    from collections import Counter

    type_counts = Counter(e["event_type"] for e in events)
    return {
        "unit_id": unit_id,
        "current_state": _state_to_dict(state) if state else None,
        "event_count": len(events),
        "event_counts_by_type": dict(type_counts),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
    }


def _state_to_dict(s: WorkUnitState) -> dict:
    return {
        "unit_id": s.unit_id,
        "feature_id": s.feature_id,
        "status": s.status,
        "branch": s.branch,
        "pr_number": s.pr_number,
        "review_round": s.review_round,
        "last_activity": s.last_activity,
        "last_error": s.last_error,
        "has_coder_session": bool(s.coder_session_id),
        "has_tester_session": bool(s.tester_session_id),
        "has_reviewer_session": bool(s.reviewer_session_id),
    }


# --------------------------- cached resources (agent + env reuse) ---------------------------


MAX_CACHE_AGE_DAYS = 30
"""How long a cached (agent_id, environment_id) stays usable before forced refresh.

Time-based invalidation lets Anthropic's underlying improvements roll in
automatically without manual intervention. Override per-call via the
max_age_days kwarg, or change this constant.
"""


def get_cached_resource(
    role: str, prompt_hash: str, *, max_age_days: int = MAX_CACHE_AGE_DAYS
) -> tuple[str, str] | None:
    """Look up (agent_id, environment_id) for (role, prompt_hash).

    Returns None if no row exists OR if the cached row is older than
    `max_age_days`. Aged-out rows are treated as cache misses; the caller
    will create fresh resources and INSERT OR REPLACE will overwrite them.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT agent_id, environment_id, created_at FROM cached_resources WHERE role = ? AND prompt_hash = ?",
            (role, prompt_hash),
        ).fetchone()
    if not row:
        return None

    try:
        created = datetime.fromisoformat(row["created_at"])
    except (ValueError, TypeError):
        return None  # malformed; treat as miss

    age = datetime.now(UTC) - created
    if age.days > max_age_days:
        return None  # aged out; force refresh

    return (row["agent_id"], row["environment_id"])


def save_cached_resource(role: str, prompt_hash: str, agent_id: str, environment_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cached_resources
                (role, prompt_hash, agent_id, environment_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (role, prompt_hash, agent_id, environment_id, _now()),
        )


def clear_cached_resources() -> int:
    """Drop every row from cached_resources. Returns the count deleted.

    Routine changes (prompt edits, model swap, env_config edit) are detected
    automatically by the resource signature in agents.py — you do NOT need
    this for those. Use only for edge cases:
      - Anthropic deprecates a model/feature that breaks existing agents
      - Cached agent_id became invalid for some reason
      - Manual debugging
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM cached_resources")
        return cur.rowcount


# --------------------------- verified repos ---------------------------


def save_verified_repo(result: VerificationResult) -> None:
    """Persist a passed VerificationResult to the verified_repos cache.

    Caller MUST check `result.passed` before calling. Idempotent — replaces
    any prior row for the same repo_url.
    """
    if not result.passed:
        raise ValueError("refusing to cache a failed verification")

    check_lookup = {c.name: c for c in result.checks}

    def _passed(name: str) -> int:
        c = check_lookup.get(name)
        return 1 if (c is not None and c.passed) else 0

    def _approvals() -> int:
        # The "≥1 approving review required" check stashes the count
        # in `detail` like "required_approving_review_count = 1"
        c = check_lookup.get("≥1 approving review required")
        if c is None or not c.detail:
            return 0
        import re as _re

        m = _re.search(r"=\s*(\d+)", c.detail)
        return int(m.group(1)) if m else (1 if c.passed else 0)

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO verified_repos (
                repo_url, default_branch, auth_mode, auth_identity, verified_at,
                has_branch_protection, required_approvals,
                blocks_force_push, blocks_deletion, blocks_bypass,
                has_codeowners, requires_signed_commits, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.repo_url,
                result.default_branch,
                result.auth_mode,
                result.auth_identity,
                _now(),
                _passed("branch protection exists"),
                _approvals(),
                _passed("force push blocked"),
                _passed("deletion blocked"),
                _passed("admin bypass blocked"),
                # CODEOWNERS moved from warnings → notes in the
                # "reviewer-as-pre-screener" pivot (it's a positive signal,
                # not a warning). See models.VerificationResult.notes.
                1 if any("CODEOWNERS" in n for n in result.notes) else 0,
                1 if any("required_signatures" in w for w in result.warnings) else 0,
                json.dumps(result.warnings),
            ),
        )


def _verified_repo_from_row(row: sqlite3.Row) -> VerifiedRepo:
    """Map a `verified_repos` SQL row to a `VerifiedRepo` dataclass.

    Can't use the `Type(**dict(row))` shortcut that Feature and
    WorkUnitState use — SQLite stores the policy booleans as INTEGER
    and the dataclass declares them as `bool`. This helper centralizes
    the int→bool coercion so add-a-column edits only touch one place.
    """
    return VerifiedRepo(
        repo_url=row["repo_url"],
        default_branch=row["default_branch"],
        auth_mode=row["auth_mode"],
        auth_identity=row["auth_identity"],
        verified_at=row["verified_at"],
        has_branch_protection=bool(row["has_branch_protection"]),
        required_approvals=row["required_approvals"],
        blocks_force_push=bool(row["blocks_force_push"]),
        blocks_deletion=bool(row["blocks_deletion"]),
        blocks_bypass=bool(row["blocks_bypass"]),
        has_codeowners=bool(row["has_codeowners"]),
        requires_signed_commits=bool(row["requires_signed_commits"]),
        warnings_json=row["warnings_json"],
    )


def get_verified_repo(repo_url: str) -> VerifiedRepo | None:
    """Return the cached VerifiedRepo for a URL, or None if absent.

    Does NOT check TTL — use `get_fresh_verified_repo` for that.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM verified_repos WHERE repo_url = ?", (repo_url,)
        ).fetchone()
    return _verified_repo_from_row(row) if row else None


def get_fresh_verified_repo(
    repo_url: str, *, ttl_hours: int = VERIFY_TTL_HOURS
) -> VerifiedRepo | None:
    """Return the cached VerifiedRepo iff present AND within TTL.

    Returns None for both "never verified" and "verified but stale". Callers
    that get None should re-verify before allowing action against the repo.
    """
    cached = get_verified_repo(repo_url)
    if cached is None:
        return None
    try:
        verified_at = datetime.fromisoformat(cached.verified_at)
    except (ValueError, TypeError):
        return None  # malformed timestamp; treat as miss
    age = datetime.now(UTC) - verified_at
    if age.total_seconds() > ttl_hours * 3600:
        return None  # stale
    return cached


def list_verified_repos() -> list[VerifiedRepo]:
    """Return every row in verified_repos, oldest verification first."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM verified_repos ORDER BY verified_at").fetchall()
    return [_verified_repo_from_row(r) for r in rows]


def forget_verified_repo(repo_url: str) -> bool:
    """Remove a row from verified_repos. Returns True if a row was deleted."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM verified_repos WHERE repo_url = ?", (repo_url,))
        return cur.rowcount > 0
