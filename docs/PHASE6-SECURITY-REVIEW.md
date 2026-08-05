# Phase 6 Security Review

## A8 Browser Authority Boundary (2026-08-05)

Verdict: PASS

Scope: only the cleanup browser/API boundary. This review did not run cleanup,
deploy a runtime, create a marker, or contact ports 33010 or 33018.

### Independent Test Verification

- Files checked: `api/genbox_push.py`, `api/support.py`.
- Test file supplemented: `tests/test_genbox_push_api.py`.
- Actual command: `python -m unittest tests.test_genbox_push_api -v`.
- Result: `Ran 8 tests ... OK`.
- The real FastAPI `TestClient` rejected forged `path`, `file_path`, `sha256`,
  `source_sha256`, `receipt`, `destination`, `destination_scope`, `source_id`,
  `environment_class`, `capability`, `cleanup_capability`, `marker`,
  `execute_marker`, `attestation`, and `runtime_identity` through JSON, query
  parameters, and `X-GenBox-*` headers on cleanup settings, preview, and run.
  Cross-site Origin requests were also rejected. The preview and execute stubs
  remained at zero calls for all rejected requests.

### Independent Read-Only Review

- Files checked: `api/genbox_push.py`, `api/support.py`,
  `services/genbox_push_cleanup.py`, `services/genbox_push_service.py`,
  `services/json_file.py`, and `tests/test_genbox_push_api.py`.
- Actual commands:
  - `Test-Path .codegraph` -> `CODEGRAPH_ABSENT`.
  - `& .\\.venv-a9-final\\Scripts\\python.exe -m unittest -v tests.test_genbox_push_api`
    -> `Ran 8 tests ... OK`.
- Findings: cleanup request models forbid extra JSON fields; all cleanup POST
  routes reject query parameters and `X-GenBox-*` headers before any cleanup
  service call; preview/run use no request-supplied cleanup evidence. The
  service reconstructs authority solely from server-side settings and state,
  then fails closed when receipt, destination scope, source identity, hash, or
  runtime identity validation is absent or invalid.

### Residual Risk

This gate covers browser-controlled input, not a host attacker with write access
to the cleanup state/data directory. That threat remains dependent on deployment
file permissions and runtime isolation. No browser-input bypass was found.

Phase 6 is paused after A8. Do not proceed to a further gate, deployment,
cleanup execution, or marker creation from this record.
