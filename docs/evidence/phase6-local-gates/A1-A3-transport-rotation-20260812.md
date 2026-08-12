# Phase 6 Local Evidence: A1-A3 Transport and Rotation

Date: 2026-08-12

Scope: Windows local verification in this sender worktree at the commit named
at the verified candidate commit `9ad6570`. Inputs were synthetic test bytes, synthetic identifiers,
and one randomly assigned loopback HTTP test server. No remote receiver,
credentials, user media, cleanup execution, deletion outside test temporary
directories, or protected ports were used.

## Result

| Gate | Local result | Evidence |
| --- | --- | --- |
| A1: forged receipt | PASS | Probe and push receipts require the contract version, configured source identity, content digest, and a success status. The characteristic source-identity forgery test proves no sender state or cleanup receipt is recorded. Existing mismatched-digest and duplicate-field tests remain green. |
| A2: redirect/downgrade | PASS | Both probe GET and push POST set TLS verification and disable redirect following. 3xx responses fail before state or cleanup receipt persistence. Cleanup-enabled configuration rejects an HTTP destination before any request. |
| A3: destination / Push-key rotation replay | PASS | The destination scope is a one-way digest of base URL, source identity, and Push key. A captured transfer refuses to send after rotation; cleanup rejects a previously recorded receipt after rotation and rechecks scope after final inspection. |

## Focused Verification

Command (Windows):

```text
uv run python -m unittest -v tests.test_genbox_push_service tests.test_genbox_push_transfer tests.test_genbox_push_cleanup.GenBoxPushCleanupTests.test_destination_rotation_invalidates_old_receipt tests.test_genbox_push_cleanup.GenBoxPushCleanupTests.test_destination_rotation_after_final_inspection_retains_source
```

Result: 33 passed, 0 skipped. The test server exercised only `127.0.0.1` and
a temporary directory.

## Limits

This is local transport evidence only. It does not authorize an isolated
execute gate, remote operation, cleanup execution, deployment, release, or a
Phase 6 completion claim.
