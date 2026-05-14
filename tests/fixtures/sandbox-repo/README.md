# sandbox-repo

Tiny fixture used by `tests/e2e/test_docker_worker_smoke.py` (F-001-U-6).

This is the `--workdir` source the Docker worker container mounts at
`/workspace` when the E2E smoke test spawns a real worker against a
real Docker daemon. The contents don't matter much — the test only
needs *some* directory to bind-mount; readability is the goal.

Touch this file only if the E2E test stops being able to find an
expected fixture file (in which case keep the change minimal).
