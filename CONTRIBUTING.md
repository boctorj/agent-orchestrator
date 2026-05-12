# Contributing to agent-orchestrator

Conventions, workflow, and quality gates for changes to this repo. Read
this before opening a PR.

For the *why* of how this is built — architecture, design choices, trust
boundaries — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). This file
covers *how to work on it*.

> [`CLAUDE.md`](CLAUDE.md) in this repo is something different: the
> *runtime persona* loaded when Claude Code launches via `orchestrator run`.
> It's not contributor guidance — it's the project lead agent's prompt.

## Canonical references — read first

- **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — the canonical engineering doc.
  What this is, how it's structured, why the design choices. Read this
  before any non-trivial change.
- **[`SECURITY.md`](SECURITY.md)** — trust model, defenses, explicit non-defenses.
  Check before touching `agents.py` networking, GitHub auth, or anything
  that crosses a sandbox boundary.
- **[`BACKLOG.md`](BACKLOG.md)** — deferred work. Check before proposing a
  new feature; it may already be planned with context.
- **[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)** — user-facing issues + fixes.
  Update this when you change failure-mode behavior.
- **`examples/01-hello-pdf.md`, `02-math-utils.md`, `03-palindrome-trap.md`** —
  three end-to-end feature flows. Useful for testing changes against a
  realistic input.

## Dev install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install        # wires .git/hooks/pre-commit
```

The `[dev]` extras pull `pytest`, `pytest-cov`, `pytest-timeout`, `ruff`,
`mypy`, `bandit`, `pre-commit`, and `types-pyyaml`.

## Repo layout (where things live)

```
orchestrator/
  cli.py                — `orchestrator init/doctor/run/dashboard/version` (Click + Rich)
  mcp_server.py         — stdio MCP entrypoint; main() inits DB + serves
  mcp_launcher.py       — minimal-env launcher that re-execs mcp_server (security)
  agents.py             — Worker protocol + ManagedAgentWorker (Anthropic Managed Agents)
  state.py              — sqlite layer (single state.db at repo root)
  models.py             — dataclasses: Feature, Plan, WorkUnit, WorkUnitState
  github.py             — gh CLI / PAT helpers
  github_app.py         — App JWT + installation token mint (1-hr, cached)
  ntfy.py               — phone push notifications
  costs.py              — session-hour cost estimates from event timestamps
  dashboard.py          — TUI (Rich Live) + markdown rendering
  prompts/
    coder.md / tester.md / reviewer.md   — agent role prompts (cache key)
  tools/                — MCP tools, one file per subdomain
    __init__.py         — FastMCP instance + shared regex/helpers
    planning.py         — load_feature, save_plan, approve_plan, list_features, get_plan
    execution.py        — spawn_unit/tester/reviewer, address_review, cycle_review, send_to_unit
    scheduling.py       — next_ready_units(_all), parallel_units(_global)
    observability.py    — get_unit_status, list_units, unit_history, unit_summary, show_dashboard
    ops.py              — hello_world_test, check_unit_pr, list_in_flight, resume_unit, reset_cached_resources

tests/                  — pytest; one test file per source module
  conftest.py           — shared fixtures (tmp_state_db, with/no_github_token, with/no_ntfy_topic)
  test_<module>.py      — mirrors orchestrator/<module>.py

docs/ARCHITECTURE.md    — canonical engineering reference (13 sections)
scripts/                — dashboard.sh, snapshot_state.sh, reset_cache.sh
.github/workflows/ci.yml — Linux + macOS + Windows × Python 3.11 + 3.12
.mcp.json               — registers the orchestrator MCP server with Claude Code
.pre-commit-config.yaml — file hygiene + ruff lint/format + private-key detection
```

## TDD workflow (required)

1. **Read the relevant module + its existing tests** before writing anything.
2. **Write a failing test** that captures the new behavior or the bug.
   Place it in `tests/test_<module>.py`.
3. **Run it — confirm it fails.** Proves you're testing the right thing.
   (Skipping this step is how you ship a passing-but-meaningless test.)
4. **Implement the minimum** code to make it pass.
5. **Run the full suite** — `pytest --no-cov` for fast iteration, `pytest`
   to also see the coverage report.
6. **Run the static gates** locally — `pre-commit run --all-files`,
   `mypy orchestrator`, `bandit -q -c pyproject.toml -r orchestrator`.
7. **Update docs** if user-visible behavior or conventions changed:
   `README.md`, `docs/ARCHITECTURE.md`, `TROUBLESHOOTING.md`, this file.
   Do **not** create new doc files unless explicitly asked.

### Test conventions

- **One test file per source module** — `tests/test_<module>.py`.
- **Use the `tmp_state_db` fixture** for anything that hits SQLite. It
  monkeypatches `state.STATE_DB` to a temp path and calls `init_db()` —
  tests never touch the real `state.db` in the repo root.
- **Use `with_github_token` / `no_github_token`** fixtures for auth tests —
  these clear App env vars from the developer's shell so the test
  environment is reproducible.
- **Mock the SDK boundary** — patch `anthropic.Anthropic`, `httpx.get`,
  `subprocess.run`, etc. Tests must not make real network calls.
- **Cover happy + expected failure + edge case** per public function.
- **80% line coverage gate is CI-enforced.** Current ~86%.

## Where to add common things

| Change | File | Test file |
|---|---|---|
| **New MCP tool** | Pick the right subdomain in `orchestrator/tools/<group>.py`, decorate with `@mcp.tool()`. Re-export in `mcp_server.py`'s import block so registration fires. | `tests/test_tools_<group>.py` |
| **New state operation** | `orchestrator/state.py`. Use the `with _connect() as conn:` pattern — the helper is a `@contextmanager` that commits AND closes. | `tests/test_state.py` |
| **New Worker backend** (Bedrock, Vertex, local) | Implement the `Worker` protocol in `orchestrator/agents.py`. See ARCHITECTURE.md §11. | `tests/test_agents.py` |
| **Edit agent behavior** | `orchestrator/prompts/{coder,tester,reviewer}.md`. The cache key includes the prompt hash, so changes auto-invalidate. | N/A — runtime behavior |
| **New CLI command** | `orchestrator/cli.py` — add a `@cli.command()` function. | `tests/test_cli.py` using `CliRunner` |
| **New dashboard panel** | `orchestrator/dashboard.py` — add a `_<panel>_data()` query + `_<panel>_panel()` Rich Panel. | `tests/test_dashboard.py` |

## Code style

- **Python 3.11+ syntax** — `from __future__ import annotations` at the top
  of every source file; use `X | None`, `list[X]`, `dict[X, Y]`.
- **Type hints required** on public functions; encouraged everywhere.
- **Google-style docstrings** on public functions/classes.
- **ruff** is both linter and formatter (line length 100, target py311).
  Config lives in `pyproject.toml [tool.ruff]`.
- **mypy** with gradual strictness — `check_untyped_defs=true` but
  `disallow_untyped_defs` is not yet on. New code should be fully typed.
- **No `print()` in `state.py` / `agents.py` / MCP tools** — return
  structured data; the lead surfaces output via MCP tool responses. CLI
  + dashboard are the only modules that print.
- **No emoji in source files** unless the user asks. The runtime lead
  uses emoji in chat output (`✅`, `🚨`, `🔄`) — those live in prompts and
  format strings, not in module docstrings or comments.

## Quality gates (CI-enforced)

| Gate | Command | Where it runs |
|---|---|---|
| Tests pass | `pytest` | every push/PR |
| Coverage ≥ 80% | `pytest --cov-fail-under=80` | CI only |
| Lint | `ruff check orchestrator tests` | pre-commit + CI |
| Format | `ruff format --check orchestrator tests` | pre-commit + CI |
| Type check | `mypy orchestrator` | CI |
| Security | `bandit -q -c pyproject.toml -r orchestrator` | CI |
| File hygiene | trailing-ws, EOF, YAML/TOML, merge-conflict, private-key | pre-commit + CI |

Run all gates locally before commit:

```bash
pre-commit run --all-files
pytest
mypy orchestrator
bandit -q -c pyproject.toml -r orchestrator
```

## Commit conventions

Format: `<type>: <subject>` where `<type>` is one of `fix`, `feat`, `docs`,
`test`, `ci`, `chore`, `refactor`, `perf`.

- **Subject**: imperative phrase, no trailing period, target ≤ 72 chars.
- **Body**: explain the **why** (not the what — diff shows the what).
  Include before/after numbers for performance, coverage, or quality fixes.
- **One concern per commit**. Splitting a 4-issue fix is fine; bundling
  unrelated changes is not.

Examples (from this repo's history):

```
fix: 4 fresh-install rough edges found in new-user walkthrough
docs: comprehensive ARCHITECTURE.md (canonical engineering reference)
test: push coverage to 87.46% + enforce 80% gate in CI
ci: pre-commit hooks + Windows CI matrix + clean build/ artifacts
feat: minimum-env MCP launcher (cross-platform)
```

## Hard rules (security invariants)

These are not negotiable. Violating any of them means the change is wrong,
regardless of how convenient it would be.

- **NEVER add a `merge_pr` MCP tool.** The "no merge" guarantee is enforced
  by three layers: branch protection on `main`, role-prompt hard rules, and
  the deliberate absence of this tool. If you find yourself wanting one,
  stop and re-read SECURITY.md §"Trust boundaries".
- **NEVER widen the network allowlist** in `agents.py` without justifying
  the new host in the commit body. Each entry is a potential exfiltration
  channel.
- **NEVER widen GitHub App / PAT permissions** beyond what the tool needs.
  Document the permission set in `README.md` if you change it.
- **NEVER read secrets at module load.** `os.getenv("API_KEY")` belongs
  inside functions, not at module top level. Module imports must be pure.
- **NEVER add other module-level side effects** (file/DB writes, network
  calls). Importability must not change the filesystem. (We hit this
  exact bug in `mcp_server.py` — `state.init_db()` at import time made
  `orchestrator doctor` silently create state.db. Fixed by moving into
  `main()`.)

## Common pitfalls (real bugs we've already hit)

- **`with sqlite3.Connection as conn:` commits but does NOT close** —
  the connection lingers until GC, leaking file descriptors. Always go
  through the `state._connect()` helper, which is a `@contextmanager`
  that yields inside `with conn:` and closes in a `finally`.
- **`state.STATE_DB` is captured by value at module load** — when writing
  tests, use the `tmp_state_db` fixture (which monkeypatches the path
  before any module caches it). Setting `STATE_DB` in a regular test
  body is too late if a tested module already grabbed it.
- **Click's `hide_input=True` reads from `/dev/tty` by default** — trips
  scripted/CI testing. Existing code in `cli.py:init` works around it via
  `CliRunner(input=...)`; new prompts should be tested the same way.
- **The agent cache key includes prompt + model + role hash** but NOT the
  networking config or tools list. If you change those, run
  `./scripts/reset_cache.sh` or have the lead call `reset_cached_resources`.
- **`getpass` warns when stdin is piped** (`GetPassWarning: Can not control
  echo`). Cosmetic; safe to ignore in test output.
- **Background tasks (Bash with `run_in_background`)** can be killed by
  `pkill -f <pattern>` matching too aggressively. Be specific when cleaning
  up dashboards or watchers.

## Workflow before opening a PR

```bash
pytest                                  # all green, coverage ≥ 80%
pre-commit run --all-files              # style + hygiene
mypy orchestrator                       # type-clean
bandit -q -c pyproject.toml -r orchestrator    # no new findings
git log --oneline -5                    # confirm commit messages match style
```

If any of those fail, fix before pushing — CI will block the merge anyway,
and the feedback loop is much shorter locally.
