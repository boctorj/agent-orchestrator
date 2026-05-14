# sandbox fixture

Sandbox fixture for the F-001-U-6 E2E smoke test
(`tests/e2e/test_docker_worker_smoke.py`).

This directory is the `--workdir` source the Docker worker container
bind-mounts at `/workspace` when the E2E smoke test spawns a real
worker against a real Docker daemon. Its contents are intentionally
minimal — the suite just needs *some* directory to mount; the
specific files aren't important beyond what the assertions check.

**Assertion contract:** `test_worker_spawns_container_against_sandbox`
runs `cat /workspace/README.md` and asserts the literal substring
`sandbox fixture` appears in the output. If you rename that phrase on
either side, update both the assertion and this README to keep them in
sync (the spec-compliance check in `tests/test_f001_u6_spec.py::
TestSandboxRepoFixture::test_sandbox_repo_readme_contains_assertion_substring`
pins this contract).

Touch this file only if the E2E test stops being able to find an
expected fixture file (in which case keep the change minimal).
