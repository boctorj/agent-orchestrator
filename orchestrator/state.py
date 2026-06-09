"""SQLite state layer for the orchestrator.

Single-file DB at the project root. All MCP tools read/write through here.
Work units are stored as JSON inside the plans table for now; Stage 3 may
split to a dedicated units table for per-unit status tracking.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.models import (
    ACTIVE_UNIT_STATUSES,
    READY_TO_MERGE_STATUSES,
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

    Race-safe: if two processes call `init_db()` concurrently (e.g.
    `orchestrator doctor` while the MCP server is starting), both may
    observe the column as missing and race the ALTER. The second one
    raises `OperationalError: duplicate column name`, which we catch
    and treat as success — the desired post-condition (column exists)
    holds regardless of which racer won.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(features)").fetchall()}
    if "ultrareview_enabled" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE features ADD COLUMN ultrareview_enabled INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


def _migrate_work_units_cancel_owner(conn: sqlite3.Connection) -> None:
    """Add F-016 Phase 2.5 columns to ``work_units`` for pre-Phase-2.5 DBs.

    ``cancelled_at`` (nullable TEXT) records when ``cancel_unit`` ran;
    ``owner`` (TEXT, default '') is the daemon/lead claim slot used by
    ``lead_advance_lock`` and by the Phase 3 daemon's terminal-advance
    CAS. SQLite has no ``ADD COLUMN IF NOT EXISTS`` so we PRAGMA-probe
    each column first and treat ``duplicate column name`` as success
    (mirrors :func:`_migrate_features_ultrareview` race-safety).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(work_units)").fetchall()}
    if "cancelled_at" not in cols:
        try:
            conn.execute("ALTER TABLE work_units ADD COLUMN cancelled_at TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    if "owner" not in cols:
        try:
            conn.execute("ALTER TABLE work_units ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise


def _migrate_work_units_conflict_fix_attempts(conn: sqlite3.Connection) -> None:
    """Add F-018 ``conflict_fix_attempts`` to ``work_units`` for pre-F-018 DBs.

    A separate counter from ``review_round`` so a sibling-merge-induced
    rebase loop doesn't consume the quality cap-3 budget — merge
    conflicts are mechanical, not a quality failure. Capped at the same
    ``CAP_3`` via :func:`orchestrator.tools.execution._conflict_fix_loop`
    and the daemon's conflict-fix dispatch.

    Race-safe in the same shape as :func:`_migrate_features_ultrareview`
    (PRAGMA-probe → ALTER → swallow ``duplicate column name``).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(work_units)").fetchall()}
    if "conflict_fix_attempts" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE work_units ADD COLUMN conflict_fix_attempts INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


def _migrate_unit_events_dedupe_key(conn: sqlite3.Connection) -> None:
    """Add `dedupe_key` + its UNIQUE index to `unit_events` for pre-F-016 DBs.

    Phase 0 of F-016 makes terminal-marker recording idempotent: callers
    pass a deterministic ``dedupe_key`` (sha256 over
    ``session_id|cycle_number|event_type|marker_payload``) and the INSERT
    becomes ``INSERT OR IGNORE`` so a duplicate scan of the same session
    response is a no-op. The dedupe column is nullable so legacy event
    types (``spawn_coder``, ``coder_resumed``, ``merged``, …) keep
    inserting a fresh row every call — only structured marker events opt
    in.

    SQLite's UNIQUE constraint treats NULL as distinct from every other
    NULL, so the partial-uniqueness contract ("at most one row per
    non-null key") falls out of the index definition without needing a
    ``WHERE dedupe_key IS NOT NULL`` clause. We add one anyway for
    clarity and to keep the index small on long-running DBs.

    Race-safe in the same shape as `_migrate_features_ultrareview` —
    PRAGMA-probe first; treat ``duplicate column name`` as success.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(unit_events)").fetchall()}
    if "dedupe_key" not in cols:
        try:
            conn.execute("ALTER TABLE unit_events ADD COLUMN dedupe_key TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_unit_events_dedupe_key "
        "ON unit_events(dedupe_key) WHERE dedupe_key IS NOT NULL"
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
                unit_id                 TEXT PRIMARY KEY,
                feature_id              TEXT NOT NULL,
                status                  TEXT NOT NULL DEFAULT 'pending',
                branch                  TEXT NOT NULL DEFAULT '',
                pr_number               INTEGER,
                coder_session_id        TEXT NOT NULL DEFAULT '',
                tester_session_id       TEXT NOT NULL DEFAULT '',
                reviewer_session_id     TEXT NOT NULL DEFAULT '',
                review_round            INTEGER NOT NULL DEFAULT 0,
                last_activity           TEXT NOT NULL DEFAULT '',
                last_error              TEXT NOT NULL DEFAULT '',
                cancelled_at            TEXT,
                owner                   TEXT NOT NULL DEFAULT '',
                conflict_fix_attempts   INTEGER NOT NULL DEFAULT 0,
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
                dedupe_key    TEXT,
                FOREIGN KEY (unit_id) REFERENCES work_units(unit_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_unit_events_unit_ts
                ON unit_events(unit_id, ts);
            -- The unique index on ``dedupe_key`` lives in
            -- ``_migrate_unit_events_dedupe_key`` so legacy DBs that
            -- pre-date the column can ALTER first, then index. Same
            -- step runs on fresh DBs (IF NOT EXISTS keeps it a no-op).

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

            -- F-016 Phase 3 watcher-daemon singleton. One row per
            -- workspace (keyed by the absolute state.db path) so two
            -- daemons targeting different workspaces coexist; two
            -- daemons targeting the same workspace contend for the row
            -- and the loser bails. Heartbeat-based takeover lets a
            -- restarted daemon reclaim a row left by a crashed instance
            -- after :data:`DEFAULT_DAEMON_LOCK_STALE_AFTER_S`.
            CREATE TABLE IF NOT EXISTS daemon_locks (
                state_db_path TEXT PRIMARY KEY,
                holder_id     TEXT NOT NULL,
                heartbeat_at  TEXT NOT NULL,
                started_at    TEXT NOT NULL
            );
            """
        )
        _migrate_features_ultrareview(conn)
        _migrate_work_units_cancel_owner(conn)
        _migrate_work_units_conflict_fix_attempts(conn)
        _migrate_unit_events_dedupe_key(conn)


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


def get_plan_with_ultrareview(feature_id: str) -> tuple[Plan, bool] | None:
    """Like `get_plan`, but JOINs the parent feature's `ultrareview_enabled`.

    Saves a round-trip vs. `get_plan() + get_feature()` for callers that
    need both. The parent row is guaranteed to exist (FK CASCADE on
    `plans → features`), so the JOIN can't yield a half-row.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT p.units_json, p.status, p.approved_at,
                   f.ultrareview_enabled
            FROM plans p
            JOIN features f ON f.id = p.feature_id
            WHERE p.feature_id = ?
            """,
            (feature_id,),
        ).fetchone()
    if not row:
        return None
    units = [WorkUnit(**u) for u in json.loads(row["units_json"])]
    plan = Plan(
        feature_id=feature_id,
        units=units,
        status=row["status"],
        approved_at=row["approved_at"],
    )
    return plan, bool(row["ultrareview_enabled"])


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
    """Insert or update the ``work_units`` row for ``unit``.

    **Columns managed via dedicated helpers, never overwritten by upsert:**

      * ``cancelled_at`` and ``owner`` (F-016 Phase 2.5) — managed via
        :func:`cancel_unit` / :func:`lead_advance_lock` /
        :func:`claim_unit_owner`.
      * ``conflict_fix_attempts`` (F-018) — managed via
        :func:`increment_conflict_fix_attempts`. The column has
        ``DEFAULT 0`` so a fresh INSERT picks up zero; the UPDATE list
        omits it so a stray upsert (with a default-zero ``WorkUnitState``)
        does not silently reset a unit mid-rebase-loop back to zero.

    A stray ``upsert_unit_state`` call from elsewhere in the codebase
    (which constructs a fresh :class:`WorkUnitState` from the model
    defaults) must not silently clear a sticky cancel, break the
    daemon's CAS, or reset the conflict counter. To deliberately
    change any of these columns, use the dedicated helper or run a
    direct SQL UPDATE.
    """
    if not unit.last_activity:
        unit.last_activity = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO work_units (
                unit_id, feature_id, status, branch, pr_number,
                coder_session_id, tester_session_id, reviewer_session_id,
                review_round, last_activity, last_error,
                cancelled_at, owner
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                unit.cancelled_at,
                unit.owner,
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


def touch_unit(
    unit_id: str,
    *,
    status: str | None = None,
    error: str = "",
    clear_error: bool = False,
) -> None:
    """Lightweight status update — bumps last_activity, optionally sets status/error.

    ``error`` is additive (empty string leaves the column alone). Pass
    ``clear_error=True`` to explicitly wipe ``last_error`` — used by
    ``reconcile_unit_pr`` when a previously-escalated unit gets merged.
    Mutually exclusive with ``error``; ``error`` wins if both are set.
    """
    with _connect() as conn:
        updates = ["last_activity = ?"]
        params: list = [_now()]
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if error:
            updates.append("last_error = ?")
            params.append(error)
        elif clear_error:
            updates.append("last_error = ?")
            params.append("")
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


def increment_conflict_fix_attempts(unit_id: str) -> int:
    """Bump the F-018 ``conflict_fix_attempts`` counter. Returns the new value.

    Mechanical-rebase retry counter kept independent of ``review_round`` so
    a sibling-induced conflict-fix doesn't burn the quality cap-3 budget,
    and a cap-3 tester-bug fix doesn't bump conflict_fix_attempts.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE work_units SET conflict_fix_attempts = conflict_fix_attempts + 1, "
            "last_activity = ? WHERE unit_id = ?",
            (_now(), unit_id),
        )
        row = conn.execute(
            "SELECT conflict_fix_attempts FROM work_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
    return row["conflict_fix_attempts"] if row else 0


def get_conflict_fix_attempts(unit_id: str) -> int:
    """Return the current ``conflict_fix_attempts`` value, or 0 if the row is missing."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT conflict_fix_attempts FROM work_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
    return row["conflict_fix_attempts"] if row else 0


# --------------------------- F-016 Phase 2.5: lead/daemon contract ----------
#
# Three primitives the lead and the (Phase 3) daemon both rely on:
#
#   * ``lead_advance_lock(unit_id)`` — context manager held during a
#     lead's ~1s ``send_to_unit_async`` submit window. The in-process
#     :class:`threading.RLock` serializes concurrent leads in the same
#     MCP server; setting ``owner='lead'`` on enter and clearing on exit
#     makes the lock visible across processes so the Phase 3 daemon
#     (separate process) sees it via ``has_active_advance_lock`` and
#     skips its ``advance_state_machine`` tick for that unit.
#
#   * ``cancel_unit`` / ``is_cancelled`` — sticky cancel. Once
#     ``cancelled_at`` is set, the unit's state machine stops — the
#     daemon's per-tick check (``if unit.cancelled_at: continue``)
#     guarantees no further transitions land.
#
#   * ``claim_unit_owner`` / ``release_unit_owner`` — atomic CAS on the
#     ``owner`` column for daemon-vs-lead terminal advances (Phase 3 uses
#     this; Phase 2.5 ships it so the daemon can adopt it without a
#     schema bump).


_UNIT_ADVANCE_LOCKS: dict[str, threading.RLock] = {}
_UNIT_ADVANCE_LOCKS_MUTEX = threading.Lock()
LEAD_OWNER = "lead"
"""``owner`` column value while a lead holds :func:`lead_advance_lock`."""


def _get_unit_advance_lock(unit_id: str) -> threading.RLock:
    """Return the process-wide :class:`threading.RLock` for ``unit_id``.

    Lazily allocates one lock per unit. The master mutex serializes
    insertion so two concurrent ``send_to_unit_async`` calls on the same
    unit get the *same* underlying lock object and one blocks on the other.
    """
    with _UNIT_ADVANCE_LOCKS_MUTEX:
        lock = _UNIT_ADVANCE_LOCKS.get(unit_id)
        if lock is None:
            lock = threading.RLock()
            _UNIT_ADVANCE_LOCKS[unit_id] = lock
        return lock


@contextmanager
def lead_advance_lock(unit_id: str) -> Iterator[None]:
    """Hold the lead's ~1s advance window for ``unit_id``.

    Serializes concurrent leads in the same MCP server via a per-unit
    :class:`threading.RLock`, and — when the ``owner`` column is
    currently free — CAS-claims ``owner='lead'`` so the Phase 3 daemon
    (separate process) sees the claim via
    :func:`has_active_advance_lock` and skips its
    ``advance_state_machine`` tick for the unit.

    **CAS, not a blind write.** If the row's ``owner`` is already held
    by something else (a Phase 3 daemon that grabbed the slot via
    :func:`claim_unit_owner`), the lead does NOT overwrite it — the
    in-process RLock still serializes concurrent leads, but the DB-side
    claim stays with whoever owned it on entry. The exit step is
    symmetric: ``owner`` is cleared *only if* the lead's CAS claim
    landed on entry. This preserves the spec's CAS contract for the
    ``owner`` column (spec § "State.db additions": ``owner`` is the CAS
    target preventing lead/daemon double-write) instead of silently
    revoking concurrent claims.

    **Nested cleanup.** The ``release_unit_owner`` call can raise on a
    SQLite hiccup (locked DB, disk full, transient backend blip). Nest
    the DB cleanup inside ``lock.release()``'s ``finally`` so the
    in-process RLock is released even when the DB write fails — a
    raised exception during cleanup must not strand every subsequent
    ``send_to_unit_async`` on a held RLock waiting forever.

    Per-unit (not per-role) per the spec: a same-tick daemon must not
    advance the reviewer while a coder is mid-receive. With the lock
    held for ~1s, the cost of unit-wide serialization is negligible.
    """
    lock = _get_unit_advance_lock(unit_id)
    lock.acquire()
    try:
        # CAS: swap ''→'lead' only when the slot is free. If a daemon
        # holds it (owner='daemon'), the lead's in-process RLock still
        # serializes concurrent leads, but the DB-side claim is left
        # untouched — preserving the daemon's CAS section.
        cas_claimed = claim_unit_owner(unit_id, LEAD_OWNER, expected_owner="")
        try:
            yield
        finally:
            if cas_claimed:
                # Release the claim we placed. Use the CAS-helper so a
                # daemon that interleaved an overwrite (it shouldn't
                # under normal flow, but the helper is defensive) is
                # left alone. The release can fail (DB locked, disk
                # full) — the outer ``finally`` below still runs and
                # releases the in-process RLock.
                release_unit_owner(unit_id, expected_owner=LEAD_OWNER)
    finally:
        lock.release()


def has_active_advance_lock(unit_id: str) -> bool:
    """True iff the row's ``owner`` column equals :data:`LEAD_OWNER`.

    The Phase 3 daemon calls this before deriving an action. The
    lock-window is ~1s so a True read corresponds to a lead actively
    submitting; the daemon should defer its tick by one cycle and
    re-read on the next poll.
    """
    s = get_unit_state(unit_id)
    return s is not None and s.owner == LEAD_OWNER


def claim_unit_owner(unit_id: str, new_owner: str, *, expected_owner: str = "") -> bool:
    """Atomic CAS on the ``owner`` column. Returns True iff the swap landed.

    Caller-supplied ``expected_owner`` is the current value the caller
    asserts (``""`` means "unowned"). ``new_owner`` lands only when
    the row's actual ``owner`` matches; otherwise the call is a no-op
    and returns False. Used by the Phase 3 daemon for terminal-advance
    races where lead and daemon both reach the same transition; the
    losing writer sees False and bails.
    """
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE work_units SET owner = ? WHERE unit_id = ? AND owner = ?",
            (new_owner, unit_id, expected_owner),
        )
        return cur.rowcount > 0


def release_unit_owner(unit_id: str, *, expected_owner: str) -> bool:
    """Conditionally clear the ``owner`` column. Returns True if it landed.

    Symmetric to :func:`claim_unit_owner` — the caller releases only if
    it still holds the claim. A False return means someone else
    (presumably the recovery path) took over.
    """
    return claim_unit_owner(unit_id, "", expected_owner=expected_owner)


def cancel_unit(unit_id: str) -> bool:
    """Sticky-cancel ``unit_id``: flip status, stamp ``cancelled_at``.

    Returns True if the row existed and was updated; False if no row
    exists (caller never spawned this unit). Idempotent: a second call
    on an already-cancelled unit is a no-op (timestamp preserved). The
    side-effects ``cancel_unit`` MCP tool layers on top — archiving
    worker sessions, recording the audit event — live in
    ``orchestrator/tools/execution.py`` so this helper stays pure-state.

    ``cancelled_at`` and ``last_activity`` share one captured timestamp
    so the cancellation row is internally consistent — two ``_now()``
    calls in one UPDATE statement can drift by microseconds on a slow
    interpreter or under GC pressure, which would make the row's two
    timestamps disagree about *when* the cancel happened.
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE work_units "
            "SET status = 'cancelled', "
            "    cancelled_at = COALESCE(cancelled_at, ?), "
            "    last_activity = ?, "
            "    owner = '' "
            "WHERE unit_id = ?",
            (now, now, unit_id),
        )
        return cur.rowcount > 0


def is_cancelled(unit_id: str) -> bool:
    """True iff the unit has been ``cancel_unit``'d.

    Reads ``cancelled_at`` rather than ``status == 'cancelled'``
    because ``cancelled_at`` is the *durable* source of truth: it is
    only ever set by :func:`cancel_unit`, never cleared by
    :func:`touch_unit`, and the SQL ``COALESCE`` clause in cancel_unit
    preserves the original timestamp across re-calls. ``status`` is the
    same signal in the happy path (cancel_unit sets both atomically
    in one UPDATE), but a future code path that flips ``status`` via
    :func:`touch_unit` without going through :func:`cancel_unit` would
    diverge from the sticky-cancel guarantee — ``cancelled_at`` stays
    pinned regardless.
    """
    s = get_unit_state(unit_id)
    return s is not None and s.cancelled_at is not None


def update_unit_deps(feature_id: str, unit_id: str, depends_on: list[str]) -> None:
    """Re-shape the DAG for ``feature_id``: replace ``unit_id``'s ``depends_on``.

    Plans are stored as ``units_json`` on the ``plans`` row; this helper
    parses the JSON, mutates the matching ``WorkUnit``, and writes back.
    Raises :class:`ValueError` if the plan is missing, the unit is not
    in the plan, or any dep references a unit not in the plan. Cycle
    detection lives in the calling MCP tool — the state layer just does
    the I/O. Orthogonal to ``work_units`` runtime state: the in-flight
    worker, if any, is untouched.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT units_json, status FROM plans WHERE feature_id = ?",
            (feature_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"no plan for {feature_id}")
        units_data = json.loads(row["units_json"])
        unit_ids = {u["id"] for u in units_data}
        if unit_id not in unit_ids:
            raise ValueError(f"unit {unit_id} not in plan for {feature_id}")
        for dep in depends_on:
            if dep not in unit_ids:
                raise ValueError(
                    f"unit {unit_id} cannot depend on unknown unit {dep} in plan {feature_id}"
                )
        for u in units_data:
            if u["id"] == unit_id:
                u["depends_on"] = list(depends_on)
                break
        conn.execute(
            "UPDATE plans SET units_json = ? WHERE feature_id = ?",
            (json.dumps(units_data), feature_id),
        )


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
    dedupe_key: str | None = None,
) -> bool:
    """Append a row to unit_events.

    Returns ``True`` if a row was inserted, ``False`` if ``dedupe_key`` was
    supplied and an event with the same key already exists.

    ``dedupe_key`` is the F-016 Phase 0 idempotency hook: terminal-marker
    callers pass a deterministic hash of
    ``session_id|cycle_number|event_type|marker_payload`` so a re-scan of
    the same worker response (e.g. the daemon re-polling an idle session)
    is a no-op rather than a duplicate audit row. When omitted, the
    INSERT writes unconditionally — preserving the original
    "never overwrites" contract for non-marker events (``spawn_coder``,
    ``coder_resumed``, ``merged``, …) that legitimately repeat.

    The UNIQUE index on ``dedupe_key`` ignores NULL rows, so the
    dedupe-vs-append branches share one table without a partial-key
    workaround.
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO unit_events
                (unit_id, feature_id, ts, event_type, source, cycle_number,
                 summary, details, session_id, dedupe_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                dedupe_key,
            ),
        )
    return cur.rowcount > 0


def list_events(unit_id: str, limit: int = 200) -> list[dict]:
    """Return events for a unit, oldest first.

    Ties on ``ts`` (the ISO-formatted timestamp) break by ``id`` —
    SQLite's primary-key autoincrement, monotonic with insertion order —
    so callers reading the timeline get a stable, insertion-faithful
    order regardless of clock resolution. ``datetime.now(UTC)`` on
    Windows can return identical microsecond strings for back-to-back
    ``record_event`` calls in a tight loop; without the secondary key
    the order of those rows is implementation-defined and breaks
    downstream readers like ``_last_reviewer_outcome`` that look for
    the most-recent marker.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM unit_events WHERE unit_id = ? ORDER BY ts, id LIMIT ?",
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
        # F-018: the mechanical-rebase retry counter is independent of
        # ``review_round`` and capped at 3; surfacing it here makes
        # ``unit_summary(unit_id)`` show "N of 3 conflict-fix retries
        # consumed" so an operator can spot a unit mid-rebase-loop
        # without reading ``unit_history`` (PR #66 M1).
        "conflict_fix_attempts": s.conflict_fix_attempts,
        "last_activity": s.last_activity,
        "last_error": s.last_error,
        "has_coder_session": bool(s.coder_session_id),
        "has_tester_session": bool(s.tester_session_id),
        "has_reviewer_session": bool(s.reviewer_session_id),
        "cancelled_at": s.cancelled_at,
        "owner": s.owner,
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


# --------------------------- F-016 Phase 3: watcher daemon ---------------------------
#
# Three helpers compose the singleton-per-workspace contract from spec
# § "Singleton enforcement — SQLite-backed, not pidfile":
#
#   * :func:`claim_daemon_lock` — atomic INSERT-or-stale-takeover. Returns
#     True only when ``holder_id`` now owns the row. Fresh holders block;
#     a row whose ``heartbeat_at`` is older than ``stale_after_s`` is
#     reclaimable.
#   * :func:`heartbeat_daemon_lock` — periodic UPDATE the live daemon
#     issues each tick. Succeeds only while the caller still holds the
#     row, so a daemon that loses the lock to a takeover sees False and
#     can shut down cleanly.
#   * :func:`release_daemon_lock` — symmetric DELETE on shutdown.
#
# The shape mirrors :func:`claim_unit_owner` / :func:`release_unit_owner`
# at the unit level — pure CAS, no in-process locks. The lock is keyed by
# the absolute ``state.db`` path so multiple workspaces (per
# ``memory/user_multi_orchestrator.md``) each get their own daemon
# without coordination.


DEFAULT_DAEMON_LOCK_STALE_AFTER_S = 30
"""How old ``heartbeat_at`` must be before takeover is allowed.

Matches the spec's 30s window: at the default 5s poll interval the live
daemon refreshes 5x within the budget, leaving ample headroom for GC
pauses or a slow SQLite write. A daemon that misses six consecutive
heartbeats is assumed dead and the next caller of
:func:`claim_daemon_lock` reclaims the row."""


def claim_daemon_lock(
    state_db_path: str,
    holder_id: str,
    *,
    stale_after_s: int = DEFAULT_DAEMON_LOCK_STALE_AFTER_S,
) -> bool:
    """Atomically claim the workspace-scoped daemon lock.

    Returns ``True`` iff ``holder_id`` now owns the row. ``False`` when
    a different holder owns the row and their ``heartbeat_at`` is fresh
    (within ``stale_after_s`` of now).

    On an empty table: writes the row unconditionally (``started_at``
    set to the claim time).
    On a stale row: takes over by CAS-updating against the prior
    ``holder_id`` so two concurrent takeovers can't both win;
    ``started_at`` is reset to the takeover time (a new daemon owns
    the row, so its "start time" begins now).
    On an own row: idempotently succeeds (already-claimed → no-op
    refresh of ``heartbeat_at`` only; ``started_at`` is preserved so
    it always reflects the original daemon-start time for this
    holder, never the most-recent re-claim).

    Args:
        state_db_path: Absolute filesystem path of the workspace's
            ``state.db``. Distinct workspaces produce distinct rows.
        holder_id: Random per-daemon-start identifier (the daemon
            generates one on each launch — never reused across
            restarts).
        stale_after_s: Seconds since the live row's ``heartbeat_at``
            before takeover is permitted.
    """
    now = _now()
    try:
        now_dt = datetime.fromisoformat(now)
    except ValueError:  # pragma: no cover — _now produces a valid iso str
        now_dt = datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)

    with _connect() as conn:
        # Fast path — no row exists. SQLite's PRIMARY KEY UNIQUE collides
        # under concurrent inserts, so we catch IntegrityError and fall
        # through to the takeover branch rather than racing the SELECT.
        try:
            conn.execute(
                "INSERT INTO daemon_locks (state_db_path, holder_id, heartbeat_at, started_at) "
                "VALUES (?, ?, ?, ?)",
                (state_db_path, holder_id, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            pass

        row = conn.execute(
            "SELECT holder_id, heartbeat_at FROM daemon_locks WHERE state_db_path = ?",
            (state_db_path,),
        ).fetchone()
        if row is None:
            # Vanishingly rare race — the row was deleted between our
            # INSERT and SELECT. Caller's next tick can retry.
            return False
        if row["holder_id"] == holder_id:
            # Idempotent — already ours. Refresh the heartbeat so a
            # repeated claim_daemon_lock() acts as a manual heartbeat.
            # ``started_at`` is deliberately NOT touched: it pins the
            # original daemon-start time for this holder so an operator
            # / ``daemon status`` reading can compute the daemon's
            # uptime as ``now() - started_at``. A re-claim is just a
            # cheap heartbeat refresh, not a "new daemon".
            conn.execute(
                "UPDATE daemon_locks SET heartbeat_at = ? "
                "WHERE state_db_path = ? AND holder_id = ?",
                (now, state_db_path, holder_id),
            )
            return True

        existing_dt: datetime | None
        try:
            existing_dt = datetime.fromisoformat(row["heartbeat_at"])
        except (TypeError, ValueError):
            existing_dt = None
        if existing_dt is None:
            # Conservative — a corrupted ``heartbeat_at`` (clock skew,
            # disk corruption, hand-edit) must not silently let a new
            # daemon snipe the active row. Treat as fresh and refuse
            # takeover; an operator who genuinely wants to reclaim a
            # corrupted lock can DELETE the row by hand and re-claim.
            return False
        if existing_dt.tzinfo is None:
            existing_dt = existing_dt.replace(tzinfo=UTC)
        age = (now_dt - existing_dt).total_seconds()
        if age < stale_after_s:
            return False
        # Stale row — take over via CAS so a concurrent takeover can
        # not also win.
        cur = conn.execute(
            "UPDATE daemon_locks SET holder_id = ?, heartbeat_at = ?, started_at = ? "
            "WHERE state_db_path = ? AND holder_id = ?",
            (holder_id, now, now, state_db_path, row["holder_id"]),
        )
        return cur.rowcount > 0


def heartbeat_daemon_lock(state_db_path: str, holder_id: str) -> bool:
    """Bump ``heartbeat_at``. Returns True only while ``holder_id`` still owns the row.

    A False return is the daemon's signal that another instance took
    over — the caller should shut down cleanly rather than continue
    driving state changes.
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE daemon_locks SET heartbeat_at = ? WHERE state_db_path = ? AND holder_id = ?",
            (now, state_db_path, holder_id),
        )
        return cur.rowcount > 0


def release_daemon_lock(state_db_path: str, holder_id: str) -> bool:
    """Delete the lock row iff ``holder_id`` still holds it.

    Symmetric to :func:`claim_daemon_lock` — the conditional DELETE
    means a daemon that lost the lock to takeover can not accidentally
    release the new holder's row on its own shutdown path.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM daemon_locks WHERE state_db_path = ? AND holder_id = ?",
            (state_db_path, holder_id),
        )
        return cur.rowcount > 0


def get_daemon_lock(state_db_path: str) -> dict | None:
    """Read the current lock row, or ``None`` if no daemon holds it.

    Used by the CLI's ``orchestrator daemon status`` subcommand to
    report ``{holder_id, heartbeat_at, started_at}`` without taking the
    lock itself.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM daemon_locks WHERE state_db_path = ?", (state_db_path,)
        ).fetchone()
    return dict(row) if row else None


# --------------------------- F-016 Phase 3: active-unit query ---------------------------


def list_active_units() -> list[WorkUnitState]:
    """Every ``work_units`` row the daemon should reconcile each tick.

    Includes :data:`~orchestrator.models.ACTIVE_UNIT_STATUSES` (the agent-
    driving statuses) plus :data:`~orchestrator.models.READY_TO_MERGE_STATUSES`
    (``approved_awaiting_merge``) so F-014's ``merged → done`` reconcile
    fires once the human merges. Terminal (``done`` / ``escalated``) and
    sticky-cancel (``cancelled``) rows are excluded; the daemon's per-unit
    ``cancelled_at`` short-circuit catches cancels that races a transition.

    Ordered by ``last_activity`` so the oldest-touched units (the most
    likely to need a heartbeat or marker re-scan) reconcile first.
    """
    statuses = tuple(ACTIVE_UNIT_STATUSES | READY_TO_MERGE_STATUSES)
    placeholders = ",".join("?" * len(statuses))
    with _connect() as conn:
        # ``placeholders`` is a fixed ?,?,? string sized to the static
        # status tuple; values bind via ``statuses`` — no user input.
        rows = conn.execute(
            f"SELECT * FROM work_units WHERE status IN ({placeholders}) "  # noqa: S608  # nosec B608
            "ORDER BY last_activity",
            statuses,
        ).fetchall()
    return [WorkUnitState(**dict(r)) for r in rows]
