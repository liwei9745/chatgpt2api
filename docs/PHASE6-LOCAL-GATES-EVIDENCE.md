# Phase 6 Sender Local Gates Evidence

Date: 2026-08-08

Scope: local synthetic sender verification plus hosted cross-platform CI review. This record does not claim VPS, cross-project, user-authorization, Phase 6 completion, Phase 7, or release completion. No runtime cleanup endpoint, execute marker, ports 33010 or 33018, receiver worktree, credentials, user media, or production logs were used.

## Baseline and Result

- Sender worktree: `E:\AI\chatgpt2api-worktrees\phase6-local-gates-20260808`
- Branch: `codex/phase6-local-gates-20260808`
- Fixed baseline: `ef3d819cda08e636c595ac6b80fa0b60dbbc8fcf`
- Implementation commits: `18c4779 security: redact phase6 cleanup projections`,
  `1e4edfa ci: prevent skipped linux cleanup gate`, and `cd7205b docs: record repaired linux ci gate`.
- Receiver reference was read-only and not modified.

## Gate Matrix

| Gate | Local conclusion | Code and test evidence |
| --- | --- | --- |
| A1 | COVERED | Receipt contract, source ID, digest, status, destination scope, and local identity are revalidated. Mismatched-receipt and destination-trust tests pass. |
| A2 | COVERED | GET and POST use `allow_redirects=False`, TLS verification, and reject 3xx. Added a POST redirect test proving no state or cleanup receipt is written. |
| A3 | COVERED | Transfer scope binds destination, source ID, and Push key; cleanup rechecks it under the settings lock. Rotation tests pass. |
| A5 | COVERED on Windows synthetic paths | Strict path validation and alias checks cover traversal, symlink, hard-link, and junction paths. Added traversal, absolute, backslash, and empty path tests. Linux-only descriptor and exchange races remain EXTERNAL. |
| A6 | COVERED | Process-shared source claims plus concurrent thread/spawn-process tests permit one terminal deletion only. |
| A10 | COVERED | Mixed result reconciliation, busy-source, recovery, and audit-failure tests cover accounting. |
| A11 | PARTIAL pending redaction follow-up | Public projections use a stable opaque item ID, but the independent review found that storage artifact names could still reach persisted audit/recovery detail. The follow-up now redacts arbitrary storage detail and records only fixed reason/count values; it requires a fresh review and CI run before A11 can be COVERED. |

## Local Verification

- `uv run python -m unittest discover -s tests -p 'test_genbox_push_service.py' -v`: 24 passed.
- `uv run python -m unittest discover -s tests -p 'test_genbox_push_cleanup.py' -v`: 77 passed, 7 skipped.
- `uv run python -m unittest discover -s tests -p 'test_image_storage_cleanup.py' -v`: 8 passed, 11 skipped.
- `uv run python -m unittest discover -s tests -p 'test_phase6_generic_delete_guard.py' -v`: 7 passed.
- Focused total: 116 passed, 18 skipped. Docker/POSIX/Linux skips are not counted as PASS.
- `uv run python -m compileall -q api services scripts tests`: passed.
- `uv run python -m unittest discover -s tests -v`: 185 passed, 18 skipped.
- `git diff --check`: passed before the implementation commit.

## Independent Review and Leakage Check

Read-only review verdict: PASS. It found no cleanup authority expansion, confirmation bypass, path-boundary regression, false success result, newly skipped test, or live credential in the implementation diff. All new tests executed rather than skipped.

The tracked-source scan found only intended Push-header handling and documented placeholder labels. The implementation diff and generated test artifacts contained only synthetic fixture values; no actual credentials, user media, user paths, raw receipts, prompts, or runtime logs were added.

## Platform and CI Follow-up

- GitHub Actions run `31255390109` at commit `57ddfe0` concluded success, but its Ubuntu job executed zero tests: pytest reported `77 skipped`. The test fixture's Linux ownership-boundary setup requires `chown`, which the unprivileged hosted runner cannot perform.
- The same run's Windows job passed `70` tests with `7` expected platform/integration skips and `20` subtests. Its macOS gate passed all `3` selected A4/A7/A12 cases, and the immutable-anchor image contract passed.
- A disposable root-run Linux application container exercised the workflow's cleanup suite: `74 passed`, `3 skipped`, and `20` subtests passed. Adding `tests/test_image_storage_cleanup.py` produced `91 passed` and `5 skipped`; the skips were the three explicitly opt-in Docker integration tests plus the Windows-only junction and handle cases.
- `.github/workflows/cleanup-security.yml` now runs the Ubuntu cleanup suite through passwordless local runner elevation and writes JUnit evidence. A post-run assertion rejects an empty or entirely skipped Linux gate. Windows and macOS commands remain platform-specific.
- Post-fix GitHub Actions run `31256410855` at commit `1e4edfa` passed all four jobs. Ubuntu reported `74 passed`, `3` explicitly gated Docker-integration skips, and `20` subtests; Windows reported `70 passed`, `7` platform/integration skips, and `20` subtests; macOS reported `3 passed` with no skips; the immutable-anchor image contract passed.
- The final workflow revision runs the sender service, cleanup, and storage suites on Windows and Ubuntu, plus an eight-case A1/A2/A3/A5/A11/core-recovery matrix on macOS. macOS A6 multi-process claim and A10 mixed-result cleanup remain explicitly `EXTERNAL`: both fail their Linux/Windows filesystem assumptions on the hosted macOS runner and are not treated as passes.

## Commits and Push

- `18c4779 security: redact phase6 cleanup projections`
- Push status: branch `codex/phase6-local-gates-20260808` is pushed non-force to `experimental/codex/phase6-local-gates-20260808` on the owner's fork. A previous attempt to push `origin` was rejected with HTTP 403; no force push or alternate history rewrite was used.
- No PR, tag, release, rc branch, marker, or deployment was created.

## External Follow-up and Resume

The local Linux POSIX matrix and hosted macOS cases are now covered. The three explicitly opt-in Docker integration tests, isolated-VPS acceptance, real host authority, and human authorization remain external evidence. Resume by arranging the separately authorized Docker/VPS evidence; do not reinterpret this LOCAL/CI record as deployment or release evidence.
