# Example 2: math-utils (multi-unit DAG with dependencies)

Tests the DAG scheduler — units with dependencies execute in topo order.

## Paste into the lead chat

> Load a feature: title "math utils library", description "Create a Python
> module `math_utils.py` at repo root. It should expose four functions:
> `add(a, b)`, `sub(a, b)`, `mul(a, b)`, and `div(a, b)`. `div` must raise
> ZeroDivisionError with a clear message when b is 0. Each function gets
> a one-line docstring. Tests should cover positives, negatives, zero
> where applicable, and the div-by-zero error.". repo_path is
> https://github.com/YOU/YOUR-SANDBOX-REPO. branch_prefix is
> feature/F-001-math.

## Expected plan shape

The lead should produce something like:

```
U-1  Create math_utils.py with add()        (no deps)
U-2  Add sub()                              (depends on U-1)
U-3  Add mul()                              (depends on U-1)
U-4  Add div() with ZeroDivisionError       (depends on U-1)
U-5  Property tests + 100% coverage         (depends on U-2, U-3, U-4)
```

After U-1 merges: U-2, U-3, U-4 are ready in parallel.
After all three merge: U-5 becomes ready.

## What it exercises

- ✅ Multi-unit DAG planning
- ✅ `parallel_units` for batches of ready units
- ✅ `reconcile_unit_pr` flipping merged units to done
- ✅ `next_ready_units` finding newly-unblocked work
- ✅ The full review cycle on each unit (5x)

## Cost

Roughly $0.05-0.15 across 5 units (session-hour estimate; tokens vary).
Run `feature_cost('F-001')` after for actual numbers.
