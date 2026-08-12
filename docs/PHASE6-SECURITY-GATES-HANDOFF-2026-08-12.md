# Phase 6 Local Security Gates Handoff

Date: 2026-08-12

## Scope And Safety Boundary

This handoff is a sender-only, local evidence record for the checked-out
chatgpt2api candidate. It does not modify GenBox, contact a VPS, use a real
credential, inspect a user image or prompt, bind ports 33010, 33018, or 33019,
or invoke a cleanup endpoint. Every source file used by the exercised cleanup
tests was created in a fresh test temporary directory. Docker was available
locally and used only with synthetic data, `--network none`, a read-only
container/filesystem boundary, and no published ports.

## Candidate Lineage

- Task branch: `codex/phase6-security-gates-20260812`.
- Evidence-verified code commit: `0dce8ee55453590e30be298b072a77b7d97afd81`.
- Candidate base: `9ad657011b07e3859edee684a009c87dcbdffeab`.
- Base relationship: `3beb170` is an ancestor of both the CI-convergence line
  (`19c2fdb`) and this release-gates candidate. `9ad6570` and `19c2fdb` are
  sibling tips from `1d67d06`; the former adds local release-gate smoke coverage.
- This task adds three adversarial regression assertions and detached evidence
  records; no production implementation behavior changed.

## Reproducible Local Evidence

Windows local synthetic execution through `0dce8ee`:

- Focused A1-A3 transport/rotation matrix: `33 passed, 0 skipped` (unittest).
- Sender service, transfer, batch, and cleanup/storage pytest suite:
  `104 passed, 18 skipped, 24 subtests passed`; JUnit contained 122 cases,
  with 104 executed and 18 explicit skips.
- Full sender pytest suite: `170 passed, 18 skipped, 253 subtests passed`;
  JUnit contained 188 cases, with 170 executed and 18 explicit skips.
- Full sender unittest discovery: `187 passed, 18 skipped`.
- Syntax compilation and `git diff --check`: passed.
- The 18 Windows skips were not counted as a pass. They cover POSIX-specific
  race mechanics, Windows-only distinctions, and opt-in Docker integrations.

### Sealed Linux POSIX Evidence

At the same exact commit, a disposable `python:3.13-slim` container ran with
`--network none`, `--read-only`, the repository mounted read-only, dependencies
mounted read-only, and only `/tmp` writable. No host ports, remote endpoints,
VPS, or protected ports were reachable.

- `tests.test_image_storage_cleanup`: `18 passed, 2 skipped` (20 tests).
- `tests.test_genbox_push_cleanup`: `74 passed, 3 skipped` (77 tests).
- Combined: `Ran 97 tests`, `92 executed and passed`, `5 explicit skips`, exit 0.
- Linux-only exchange/tombstone, parent-directory ABA/move, descriptor rewrite,
  staging replacement, hard-link race, cross-process writer/claim, and crash
  recovery paths all executed. Skips were limited to Windows-only junction/
  handle cases and three opt-in Docker/FastAPI launcher tests.

The sealed image was `python:3.13-slim` resolved to repository digest
`sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91`.

The final A11 scan covered tracked source, the `9ad6570..0dce8ee` incremental
diff and history, JUnit/log evidence, and generated temporary evidence. The
incremental diff/history and JUnit/log/generated-evidence scopes had zero
high-confidence hits. Tracked-source pattern hits were reviewed by filename
only: one documented placeholder and one intentional synthetic PEM detector;
no match content is reproduced here. Temporary evidence remains untracked and
outside the commit.

## Gate Matrix

| Gate | Result | Evidence boundary |
| --- | --- | --- |
| A1 receipt forgery | PASS (local) | Receipt contract, source, digest, status and durable result validation; mismatch test passed. |
| A2 redirect/downgrade | PASS (local) | GET/POST disable redirects, require TLS verification, and reject 3xx before state/receipt persistence. |
| A3 rotation/replay | PASS (local) | Transfer scope binds destination, source ID, and Push key; captured context and cleanup rechecks reject rotation. |
| A4 identity/TOCTOU | PASS (local + sealed Linux; historical hosted matrix also recorded) | Replacement, same-inode rewrite, hard-link, parent ABA/move, exchange/tombstone, and final-identity checks retain ambiguous sources. |
| A5 alias/path traversal | PASS (Windows + sealed Linux scope) | Traversal, separator, symlink, hard-link, junction, staging, and descriptor/path-boundary cases retain the source; unsupported platform cases remain explicit skips. |
| A6 single delete ownership | PASS (Windows + sealed Linux scope) | Thread, spawned-process, write-lease, and cross-process source claims allow one terminal deletion; busy sources remain retained. |
| A7 crash/restart recovery | PASS (local + sealed Linux; historical CI also recorded) | Exchange/tombstone/index-write/restart recovery resolves ambiguity to retained or delete-unknown without automatic cleanup. |
| A8 browser authority | PASS (historical local review) | Browser/query/header authority fields are rejected; server-owned cleanup state remains authoritative. |
| A9 runtime/host attestation | PASS (design/tests; historical local Docker/CI evidence) | Marker, issuer, runtime binding, image-anchor, and launcher contracts are fail-closed; no isolated execute claim is made. |
| A10 mixed results | PASS (local) | Mixed retained/deleted outcomes and busy-source accounting reconcile totals. |
| A11 privacy | PASS (local) | Public/audit projection, smoke output, tracked source, artifacts, diff, and 200-commit history scans are redacted/clean under the stated synthetic test scope. |
| A12 bounded/slow receipts | PASS (local + sealed Linux scope; historical CI also recorded) | Slow-drip, bounded streaming, malformed receipt, timeout, and redirect paths retain sources. |

## Design And Merge Gate

- Design Gate: PASS for the sender's local design/contract boundary. A1-A12
  controls are represented by tests or dated historical CI evidence, with
  platform scope and skips explicit. This does not change GenBox's separate
  Phase 6 roadmap state: isolated acceptance, owner-clone redeployment, and
  production non-mutation remain external completion criteria.
- Merge Gate: PASS for this sender-only candidate at exact `0dce8ee`: the
  branch contains only reviewed regression/evidence commits; Windows and
  sealed Linux suites have nonzero executed counts; syntax, whitespace, and
  scoped redaction scans passed. Hosted CI for this exact SHA was not awaited,
  so this is a local merge-quality result, not release approval.

## Isolated Execute Authorization Package (Prepared Only)

No execution is authorized by this package. Execute only after all local gates
are re-run against the exact immutable commit/image, an owner explicitly
approves the exact isolated target, and an independent reviewer records PASS.

1. Verify target ownership and that source/target directories, volumes,
   container names, Compose project, ports, source ID, and Push key are unique.
2. Require a newly generated non-production Push key and cleanup capability;
   never place either in URLs, command history, Git, ordinary logs, or this
   handoff.
3. Require an immutable sender image digest, host-owned identity file,
   host-owned issuer key outside storage/artifact mounts, fresh artifact
   directory, and runtime-attestation verification.
4. Start with cleanup disabled. Exercise only synthetic media under the
   isolated storage root; prove one Push, idempotent retry, failure retention,
   and cleanup dry-run before considering any delete.
5. Stop immediately on target ambiguity, any production overlap, failed health
   check, unexpected source/receipt mismatch, secret leakage, cleanup outside
   the owned synthetic root, or failed rollback verification.
6. Roll back only owned resources: stop the isolated container, remove its
   unique project/resources, revoke the generated Push key/capability, and
   preserve sanitized evidence. Never run a broad prune or touch production.

## Candidate-Release Handoff

This file is intentionally standalone and may be summarized by the parallel
candidate-release task without changing GenBox source-of-truth documents.

- Sender candidate: `0dce8ee` on `codex/phase6-security-gates-20260812`.
- This task adds regression/evidence-only changes atop verified candidate
  `9ad6570`; no production implementation behavior changed.
- Local quality evidence is PASS for A1-A12 within the stated local,
  historical-CI, and sealed-Linux boundaries; isolated execution remains
  unexecuted and is not represented as successful.
- Do not label Phase 6, Phase 7, deployment, cleanup execution, or release as
  complete based on this handoff.
