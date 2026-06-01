# F-012: CI Poll Tuning + Reviewer Session Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce cycle_review gate latency by (1) cutting the CI poll interval and removing the redundant final gate, and (2) resuming the existing reviewer session on retry instead of spawning a cold one.

**Architecture:** Two independent units targeting `orchestrator/ci_wait.py`, `orchestrator/tools/execution.py`, and `orchestrator/tools/__init__.py`. U-1 is pure infra (env-var default + dead code deletion). U-2 adds a `_resume_reviewer_for_delta` helper inside `_reviewer_phase` that replaces the session-clear + fresh-spawn pattern with `worker.resume`.

**Tech Stack:** Python 3.11+, pytest, `orchestrator.workers.managed_agent.ManagedAgentWorker.resume(session_id, msg) -> str`

---

## Unit 1: CI Poll Interval Default 5s (allow 0) + Drop GATE 3

### Task 1: Fail a test for the new default poll interval

**Files:**
- Test: `tests/test_ci_wait.py` (add to existing file)

- [ ] **Step 1: Write the failing test**

Add this class after the existing `TestWaitForCIGreen` class in `tests/test_ci_wait.py`:

```python
class TestDefaultPollInterval:
    def test_default_poll_interval_is_5_seconds(self):
        assert ci_wait.DEFAULT_POLL_INTERVAL_SECONDS == 5

    def test_poll_interval_zero_is_allowed(self):
        """CI_WAIT_POLL_INTERVAL=0 means busy-poll — valid for tests."""
        clock = FakeClock(step=0)
        fetch = ScriptedGetChecks(
            _snapshot(_check("lint", status="in_progress", conclusion=None)),
            _snapshot(_check("lint")),
        )
        r = wait_for_ci(
            "u", 1,
            timeout_seconds=60,
            poll_interval_seconds=0,
            no_ci_grace_seconds=30,
            get_checks=fetch,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert r.status == "green"
        assert clock.slept == [0]  # slept once, with 0s interval
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /workspace
pytest tests/test_ci_wait.py::TestDefaultPollInterval -v
```

Expected: `FAILED test_default_poll_interval_is_5_seconds` (currently 15, not 5). `test_poll_interval_zero_is_allowed` should already pass since `sleep(0)` is valid.

---

### Task 2: Change the default to 5s and update docs

**Files:**
- Modify: `orchestrator/ci_wait.py:51`
- Modify: `docs/ARCHITECTURE.md:593-594`

- [ ] **Step 1: Change the default in ci_wait.py**

In `orchestrator/ci_wait.py`, change line 51 from:
```python
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("CI_WAIT_POLL_INTERVAL", "15"))
```
to:
```python
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("CI_WAIT_POLL_INTERVAL", "5"))
```

Also update the comment above it (lines 48-50) from:
```python
# Poll the check_runs API every N seconds. Long enough to avoid rate-
# limiting on long waits, short enough that we react promptly when CI
# settles.
```
to:
```python
# Poll the check_runs API every N seconds. 0 = busy-poll (valid for tests
# and repos with sub-second CI). Default 5s: reacts within one poll cycle
# on typical CI matrices without hammering the API.
```

- [ ] **Step 2: Update ARCHITECTURE.md**

In `docs/ARCHITECTURE.md`, find the line:
```
`CI_WAIT_TIMEOUT_SECONDS` (default 600s), `CI_WAIT_POLL_INTERVAL`
(default 15s), `CI_WAIT_NO_CI_GRACE` (default 30s). "No CI configured"
```
Change to:
```
`CI_WAIT_TIMEOUT_SECONDS` (default 600s), `CI_WAIT_POLL_INTERVAL`
(default 5s, 0 = busy-poll), `CI_WAIT_NO_CI_GRACE` (default 30s). "No CI configured"
```

- [ ] **Step 3: Run tests to verify they pass**

```bash
pytest tests/test_ci_wait.py::TestDefaultPollInterval -v
```

Expected: both tests PASS.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/ci_wait.py docs/ARCHITECTURE.md tests/test_ci_wait.py
git commit -m "perf: lower CI_WAIT_POLL_INTERVAL default from 15s to 5s, allow 0"
```

---

### Task 3: Fail a test that proves GATE 3 is gone

**Files:**
- Test: `tests/test_tools_execution.py` (add new test class)

- [ ] **Step 1: Understand the helper used for GATE 3**

GATE 3 is the block at `orchestrator/tools/execution.py:1084-1090`:
```python
# GATE 3 (defensive): final CI check before declaring ready-to-merge.
ok, msg = _wait_ci_with_fix_loop(ctx, "final pre-merge check")
if not ok:
    return _emit_terminal(ctx, "escalated", msg or "CI red at final pre-merge confirmation")
```

Find how `cycle_review` is tested in `tests/test_tools_execution.py` — look for the pattern that counts how many times the CI wait helper is called. GATE 3 makes it 3 CI waits on a clean run (gate 1 + gate 2 + gate 3). After deletion it will be 2.

```bash
grep -n "GATE\|gate\|final pre-merge\|wait_ci\|ci_wait" tests/test_tools_execution.py | head -20
```

- [ ] **Step 2: Write the failing test**

Add this to `tests/test_tools_execution.py`. Adapt the import and fixture patterns you see in the existing tests:

```python
class TestCycleReviewNoGate3:
    """cycle_review must NOT call a third CI wait after reviewer approves.

    Before F-012-U-1 there were 3 _wait_ci_with_fix_loop calls per clean
    cycle (coder push, tester push, final defensive). After deletion: 2.
    """

    def test_only_two_ci_gates_on_clean_cycle(self, ...):  # use same fixture sig as existing tests
        # Arrange: patch _wait_ci_with_fix_loop, _tester_phase, _copilot_phase,
        # _reviewer_phase to succeed immediately. Count how many times the CI
        # helper is called.
        ci_wait_calls = []

        def fake_ci_with_fix_loop(ctx, label):
            ci_wait_calls.append(label)
            return True, None

        # Also patch tester, copilot, reviewer to succeed without side-effects.
        # See pattern used in existing cycle_review tests.

        with patch("orchestrator.tools.execution._wait_ci_with_fix_loop", fake_ci_with_fix_loop), \
             patch("orchestrator.tools.execution._tester_phase", return_value=(True, None)), \
             patch("orchestrator.tools.execution._copilot_phase"), \
             patch("orchestrator.tools.execution._reviewer_phase", return_value=(True, None)), \
             patch("orchestrator.tools.execution.ensure_verified_for_feature", return_value=None):
            result = cycle_review("F-TEST", "F-TEST-U-1")

        assert len(ci_wait_calls) == 2, f"Expected 2 CI gates, got {len(ci_wait_calls)}: {ci_wait_calls}"
        assert "final pre-merge" not in " ".join(ci_wait_calls)
        assert json.loads(result)["outcome"] == "approved_awaiting_merge"
```

Look at the existing test fixtures in `tests/test_tools_execution.py` and adapt the test to match how they set up feature/unit state (likely via a `db_path` fixture or `tmp_path`). Mirror the setup exactly — don't invent a new pattern.

- [ ] **Step 3: Run the test to verify it fails (currently 3 gates)**

```bash
pytest tests/test_tools_execution.py::TestCycleReviewNoGate3 -v
```

Expected: FAIL — `AssertionError: Expected 2 CI gates, got 3`.

---

### Task 4: Delete GATE 3 from cycle_review

**Files:**
- Modify: `orchestrator/tools/execution.py:1084-1090`

- [ ] **Step 1: Delete the GATE 3 block**

In `orchestrator/tools/execution.py`, remove lines 1084-1090:
```python
    # GATE 3 (defensive): final CI check before declaring ready-to-merge.
    # If reviewer's loop already pushed fixes, _reviewer_phase has waited; this
    # is a belt-and-suspenders confirmation. A red here typically means a race
    # with a re-running workflow.
    ok, msg = _wait_ci_with_fix_loop(ctx, "final pre-merge check")
    if not ok:
        return _emit_terminal(ctx, "escalated", msg or "CI red at final pre-merge confirmation")

```

The result: `_reviewer_phase` is followed directly by `_emit_terminal(ctx, "approved_awaiting_merge", ...)`.

- [ ] **Step 2: Update the cycle_review docstring**

Change the docstring to remove the GATE 3 reference:
```python
    """Full automated post-spawn loop:
      tester → (if BUG: address_review → tester) → Copilot review →
      our reviewer → (if CHANGES: address_review → reviewer) → terminal.

    Cap = CAP_3 shared cycles across tester-bugs and reviewer-changes.
    On cap hit or any BLOCKED: marks escalated, returns summary.
    BLOCKS until terminal (success or escalation). Typically 5-20+ minutes.

    Repo must be fresh-verified (call `verify_repo(<url>)` if blocked).
    """
```

- [ ] **Step 3: Run all tests**

```bash
pytest tests/test_tools_execution.py tests/test_ci_wait.py -v
```

Expected: all pass. Run the full suite to check for regressions:

```bash
pytest --tb=short -q
```

Expected: same pass count as before (no tests were relying on GATE 3 behavior).

- [ ] **Step 4: Commit**

```bash
git add orchestrator/tools/execution.py tests/test_tools_execution.py
git commit -m "perf: remove GATE 3 defensive CI re-check from cycle_review"
```

---

## Unit 2: Reviewer Session Resume on Retry (Delta Review)

### Task 5: Add compose_reviewer_delta_task to tools/__init__.py

**Files:**
- Modify: `orchestrator/tools/__init__.py` (after `compose_reviewer_task` at line 202)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_init.py` (or wherever `compose_reviewer_task` is tested — find it with `grep -rn "compose_reviewer_task" tests/`):

```python
def test_compose_reviewer_delta_task_contains_required_fields():
    from orchestrator.tools import compose_reviewer_delta_task
    from orchestrator.models import Feature, WorkUnit

    feature = Feature(id="F-TST", title="Test", description="desc", repo_path="https://github.com/x/y", branch_prefix="")
    unit = WorkUnit(id="F-TST-U-1", title="Add thing", description="Add the thing")

    msg = compose_reviewer_delta_task(feature, unit, pr_number=42, fix_summary="Fixed M1: flipped flag", github_token="tok")

    assert "42" in msg
    assert "https://github.com/x/y" in msg
    assert "Fixed M1" in msg
    assert "REVIEW_RECOMMEND_MERGE" in msg
    assert "REVIEW_REQUEST_CHANGES" in msg
    assert "tok" in msg
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/ -k "compose_reviewer_delta" -v
```

Expected: `ImportError` or `FAILED` — `compose_reviewer_delta_task` doesn't exist yet.

- [ ] **Step 3: Implement compose_reviewer_delta_task**

In `orchestrator/tools/__init__.py`, add after `compose_reviewer_task` (after line 226):

```python
def compose_reviewer_delta_task(
    feature: "Feature",
    unit: "WorkUnit",
    pr_number: int,
    fix_summary: str,
    github_token: str,
) -> str:
    return f"""Delta re-review for PR #{pr_number}, unit {unit.id}.

REPO_URL:  {feature.repo_path}
PR_NUMBER: {pr_number}
GH_TOKEN:  {github_token}

UNIT TITLE: {unit.title}

FIX SUMMARY (what the coder addressed since your last review):
{fix_summary}

CI is currently green. You already have full context of this PR from
your previous review turn. Do NOT repeat the full 7-step workflow.

Focus only on:
1. Fetch the latest inline comment threads and verify your prior
   🔴 / 🟠 findings are resolved (`gh pr view {pr_number} --json reviews,comments`).
2. Fetch only the new commits since your last review:
   `gh pr diff {pr_number}` — check whether the fix introduces new 🔴 or 🟠 issues.

End with EXACTLY ONE of:
- `REVIEW_RECOMMEND_MERGE: <one-line reason>` (all prior blockers resolved, no new blockers)
- `REVIEW_REQUEST_CHANGES: <one-line main issue>` (prior blocker unresolved or new blocker found)
- `REVIEW_COMMENT` (only 🟡 / 🔵 nits remain, no blockers)
- `BLOCKED: <one-line reason>` (couldn't review)
"""
```

Also add `compose_reviewer_delta_task` to the `__all__` list at the bottom of the file (or wherever the other `compose_*` functions are exported).

- [ ] **Step 4: Run the test to verify it passes**

```bash
pytest tests/ -k "compose_reviewer_delta" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tools/__init__.py tests/
git commit -m "feat: add compose_reviewer_delta_task for session-resume reviewer retries"
```

---

### Task 6: Add _resume_reviewer_for_delta helper in execution.py

**Files:**
- Modify: `orchestrator/tools/execution.py` (add helper before `_reviewer_phase`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_execution.py`:

```python
class TestResumeReviewerForDelta:
    """_resume_reviewer_for_delta resumes the existing session instead of spawning."""

    def test_resumes_existing_session_with_delta_message(self, ...):
        # Arrange: unit has reviewer_session_id already set
        # Patch worker.resume to return "REVIEW_RECOMMEND_MERGE: clean"
        # Patch compose_reviewer_delta_task
        # Assert: worker.resume called with (reviewer_session_id, delta_msg)
        # Assert: spawn_reviewer NOT called
        # Assert: returned dict has outcome == "REVIEW_RECOMMEND_MERGE"
        ...

    def test_handles_request_changes_on_delta(self, ...):
        # Same setup but worker.resume returns "REVIEW_REQUEST_CHANGES: still broken"
        # Assert: outcome == "REVIEW_REQUEST_CHANGES"
        ...

    def test_handles_blocked_on_delta(self, ...):
        # Same setup but worker.resume returns "BLOCKED: reason=network_error | ..."
        # Assert: outcome starts with "BLOCKED"
        ...
```

Use the same DB/fixture patterns you see in the rest of `tests/test_tools_execution.py`. Import `_resume_reviewer_for_delta` directly from `orchestrator.tools.execution` — it will be a module-level private function.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_tools_execution.py::TestResumeReviewerForDelta -v
```

Expected: `ImportError` — `_resume_reviewer_for_delta` doesn't exist.

- [ ] **Step 3: Implement _resume_reviewer_for_delta**

Add this function in `orchestrator/tools/execution.py`, just before `_reviewer_phase` (around line 995):

```python
def _resume_reviewer_for_delta(
    ctx: CycleContext,
    unit_state: "UnitState",
    feature: "Feature",
    fix_summary: str,
) -> dict:
    """Resume the existing reviewer session with a delta message.

    Used by _reviewer_phase on retry instead of spawning a cold session.
    Returns the same outcome dict shape as spawn_reviewer.
    """
    from orchestrator.tools import compose_reviewer_delta_task

    unit = None
    plan = state.get_plan(ctx.feature_id)
    if plan:
        unit = next((u for u in plan.units if u.id == ctx.unit_id), None)
    if not unit:
        return {"outcome": f"RAW", "raw": f"ERROR: unit {ctx.unit_id} not in plan"}

    github_token = get_agent_token()
    delta_msg = compose_reviewer_delta_task(
        feature, unit, unit_state.pr_number, fix_summary, github_token
    )

    state.touch_unit(ctx.unit_id, status="reviewing")
    state.record_event(
        ctx.unit_id,
        ctx.feature_id,
        "spawn_reviewer",
        source="orchestrator",
        cycle_number=unit_state.review_round,
        summary=f"Resuming reviewer for {ctx.unit_id} (delta)",
    )

    try:
        worker = ManagedAgentWorker(role="reviewer")
        response = worker.resume(unit_state.reviewer_session_id, delta_msg)
    except Exception as e:  # noqa: BLE001
        state.touch_unit(ctx.unit_id, status="escalated", error=str(e))
        state.record_event(
            ctx.unit_id, ctx.feature_id, "reviewer_error",
            source="orchestrator",
            cycle_number=unit_state.review_round,
            summary=str(e),
        )
        return {"outcome": "RAW", "raw": f"ERROR resuming reviewer: {e}"}

    # Parse response using the same regex matchers as spawn_reviewer
    recommend = REVIEW_RECOMMEND_MERGE_RE.search(response)
    if recommend:
        reason = recommend.group(1).strip()
        state.touch_unit(ctx.unit_id, status="in_ci")
        state.record_event(
            ctx.unit_id, ctx.feature_id, "reviewer_recommend_merge",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=reason,
            session_id=unit_state.reviewer_session_id,
            details=tail(response),
        )
        return {"outcome": "REVIEW_RECOMMEND_MERGE", "reason": reason}

    request = REVIEW_REQUEST_CHANGES_RE.search(response)
    if request:
        issue = request.group(1).strip()
        state.touch_unit(ctx.unit_id, status="in_ci")
        state.record_event(
            ctx.unit_id, ctx.feature_id, "reviewer_request_changes",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=issue,
            session_id=unit_state.reviewer_session_id,
            details=tail(response),
        )
        return {"outcome": "REVIEW_REQUEST_CHANGES", "issue": issue}

    comment = REVIEW_COMMENT_RE.search(response)
    if comment:
        state.touch_unit(ctx.unit_id, status="in_ci")
        state.record_event(
            ctx.unit_id, ctx.feature_id, "reviewer_comment",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary="Comment-only review",
            session_id=unit_state.reviewer_session_id,
        )
        return {"outcome": "REVIEW_COMMENT"}

    blocked = BLOCKED_RE.search(response)
    if blocked:
        state.touch_unit(ctx.unit_id, status="escalated", error=blocked.group(0))
        state.record_event(
            ctx.unit_id, ctx.feature_id, "reviewer_blocked",
            source="reviewer",
            cycle_number=unit_state.review_round,
            summary=blocked.group(0),
            session_id=unit_state.reviewer_session_id,
        )
        return {"outcome": f"BLOCKED: {blocked.group(0)}"}

    # No marker
    state.touch_unit(ctx.unit_id, status="escalated", error="reviewer_no_marker (delta)")
    state.record_event(
        ctx.unit_id, ctx.feature_id, "reviewer_no_marker",
        source="reviewer",
        cycle_number=unit_state.review_round,
        summary="No terminal marker from reviewer (delta resume)",
        session_id=unit_state.reviewer_session_id,
        details=tail(response),
    )
    return {"outcome": "RAW", "raw": response[-500:]}
```

Look at `spawn_reviewer` (lines 391-530 approx) to copy the exact regex names (`REVIEW_RECOMMEND_MERGE_RE`, `REVIEW_REQUEST_CHANGES_RE`, `REVIEW_COMMENT_RE`, `BLOCKED_RE`), the `tail()` helper call, and the event type strings — use exact copies, not paraphrases.

- [ ] **Step 4: Run the tests**

```bash
pytest tests/test_tools_execution.py::TestResumeReviewerForDelta -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tools/execution.py tests/test_tools_execution.py
git commit -m "feat: add _resume_reviewer_for_delta helper for session-resume retries"
```

---

### Task 7: Wire _resume_reviewer_for_delta into _reviewer_phase

**Files:**
- Modify: `orchestrator/tools/execution.py:1026-1035` (the retry block in `_reviewer_phase`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_execution.py`:

```python
class TestReviewerPhaseResumesOnRetry:
    """_reviewer_phase must call _resume_reviewer_for_delta on retry, not spawn_reviewer."""

    def test_retry_calls_resume_not_spawn(self, ...):
        # Arrange: first spawn_reviewer returns REVIEW_REQUEST_CHANGES
        # address_review succeeds (FIX_PUSHED)
        # _wait_ci_with_fix_loop succeeds
        # Expect: _resume_reviewer_for_delta called, NOT a second spawn_reviewer
        # Expect: reviewer_session_id NOT cleared before retry

        spawn_calls = []
        resume_calls = []

        def fake_spawn_reviewer(feature_id, unit_id):
            spawn_calls.append((feature_id, unit_id))
            # First call only — returns REQUEST_CHANGES
            return json.dumps({"outcome": "REVIEW_REQUEST_CHANGES", "issue": "bad code"})

        def fake_resume_reviewer(ctx, unit_state, feature, fix_summary):
            resume_calls.append(fix_summary)
            return {"outcome": "REVIEW_RECOMMEND_MERGE", "reason": "fixed"}

        with patch("orchestrator.tools.execution.spawn_reviewer", fake_spawn_reviewer), \
             patch("orchestrator.tools.execution._resume_reviewer_for_delta", fake_resume_reviewer), \
             patch("orchestrator.tools.execution.address_review", return_value=json.dumps({"outcome": "FIX_PUSHED"})), \
             patch("orchestrator.tools.execution._wait_ci_with_fix_loop", return_value=(True, None)):
            approved, msg = _reviewer_phase(ctx)  # ctx needs a valid unit with reviewer_session_id set

        assert approved is True
        assert len(spawn_calls) == 1    # initial spawn only
        assert len(resume_calls) == 1   # delta resume on retry
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_tools_execution.py::TestReviewerPhaseResumesOnRetry -v
```

Expected: FAIL — currently `_reviewer_phase` clears the session and calls `spawn_reviewer` a second time.

- [ ] **Step 3: Modify _reviewer_phase to use resume on retry**

In `orchestrator/tools/execution.py`, find `_reviewer_phase`. Replace the retry block (lines 1026-1034):

**Before:**
```python
        # Clear reviewer session so retry creates a fresh one
        s = state.get_unit_state(ctx.unit_id)
        if s is None:
            return False, "unit state vanished mid-cycle"
        s.reviewer_session_id = ""
        state.upsert_unit_state(s)

        reviewer_out = _record_step(
            ctx, "reviewer (retry)", spawn_reviewer(ctx.feature_id, ctx.unit_id)
        )
```

**After:**
```python
        s = state.get_unit_state(ctx.unit_id)
        if s is None:
            return False, "unit state vanished mid-cycle"

        reviewer_out = _record_step(
            ctx,
            "reviewer (delta resume)",
            _resume_reviewer_for_delta(ctx, s, feature, reviewer_out.get("issue", "")),
        )
```

Note: `feature` must be in scope. Check whether `_reviewer_phase` already has access to a `feature` variable — if not, add `feature = state.get_feature(ctx.feature_id)` near the top of `_reviewer_phase` before the `spawn_reviewer` call:

```python
def _reviewer_phase(ctx: CycleContext) -> tuple[bool, str | None]:
    feature = state.get_feature(ctx.feature_id)
    if not feature:
        return False, f"feature {ctx.feature_id} not found"
    reviewer_out = _record_step(ctx, "reviewer", spawn_reviewer(ctx.feature_id, ctx.unit_id))
    ...
```

Also, `_resume_reviewer_for_delta` returns a dict directly (not a JSON string), but `_record_step` expects what `spawn_reviewer` returns (a JSON string). Check what `_record_step` does with its third argument and either:
- Have `_resume_reviewer_for_delta` return a JSON string, OR
- Pass the dict directly if `_record_step` accepts dicts

Look at `_record_step`'s signature in execution.py and match the return type accordingly.

- [ ] **Step 4: Run all related tests**

```bash
pytest tests/test_tools_execution.py -v --tb=short
```

Expected: all pass including the new `TestReviewerPhaseResumesOnRetry`.

- [ ] **Step 5: Run the full suite**

```bash
pytest --tb=short -q
```

Expected: same pass count ± new tests.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/tools/execution.py tests/test_tools_execution.py
git commit -m "perf: resume reviewer session on retry (delta review) instead of cold spawn"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] CI_WAIT_POLL_INTERVAL default dropped from 15s → 5s (Task 2)
- [x] `CI_WAIT_POLL_INTERVAL=0` documented and tested (Task 1)
- [x] GATE 3 defensive CI re-check removed from `cycle_review` (Task 4)
- [x] Reviewer session resumed on retry via `worker.resume` (Task 7)
- [x] Delta review prompt scoped to "check prior findings + new diff only" (Task 5)
- [ ] Adaptive Copilot wait — **intentionally dropped per user instruction**

**Placeholder scan:** None — all steps include real code.

**Type consistency:**
- `compose_reviewer_delta_task` signature matches all call sites in `_resume_reviewer_for_delta`
- `_resume_reviewer_for_delta` return type (dict vs JSON string) must match what `_record_step` expects — verify at Task 7 Step 3.
