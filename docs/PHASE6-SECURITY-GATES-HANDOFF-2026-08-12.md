# Phase 6 Local Security Gates Handoff

Date: 2026-08-12

## Scope And Safety Boundary

This handoff is a sender-only, local evidence record for the checked-out
chatgpt2api candidate. It does not modify GenBox, contact a VPS, use a real
credential, inspect a user image or prompt, bind ports 33010, 33018, or 33019,
or invoke a cleanup endpoint. Every source file used by the exercised cleanup
tests was created in a fresh test temporary directory. The Docker daemon was
not available to this task, so no Docker, Linux-container, or remote execution
claim is made.

## Candidate Lineage

- Task branch: `codex/phase6-security-gates-20260812`.
- Task-head commit: `079a29f3f6cc6b46f0f0466a921530992cc5d8c9`.
- Candidate base: `9ad657011b07e3859edee684a009c87dcbdffeab`.
- Base relationship: `3beb170` is an ancestor of both the CI-convergence line
  (`19c2fdb`) and this release-gates candidate. `9ad6570` and `19c2fdb` are
  sibling tips from `1d67d06`; the former adds local release-gate smoke coverage.
- This task adds three adversarial regression assertions and detached evidence
  records; no production implementation behavior changed.

## Reproducible Local Evidence

Windows local synthetic execution through `079a29f`:

- Focused adversarial matrix: 16 passed. It covers forged/mismatched receipt,
  forged source identity, POST redirect refusal, destination and Push-key rotation, traversal,
  symlink/hard-link/junction aliases, Windows hard-link race, concurrent and
  cross-process deletion claims, mixed-result accounting, busy-source
  accounting, public/audit redaction, and smoke-output redaction.
- Sender service, transfer, batch, and cleanup suite: 124 passed, 7 explicit
  platform/Docker-integration skips.
- Full sender suite: 186 passed, 18 explicit platform/integration skips.
- Syntax compilation and `git diff --check`: passed.
- The 18 skips were not counted as a pass. They cover POSIX-specific race
  mechanics and opt-in local Docker tests unavailable to this Windows task.

A secret-pattern source scan, untracked-file scan, generated-artifact scan, and
200-commit increment-history scan did not find a high-confidence real secret.
The three tracked PEM matches are intentional synthetic/private-key test
fixtures only; no match content is reproduced here.

## Gate Matrix

| Gate | Result | Evidence boundary |
| --- | --- | --- |
| A1 receipt forgery | PASS (local) | Receipt contract, source, digest, status and durable result validation; mismatch test passed. |
| A2 redirect/downgrade | PASS (local) | GET/POST disable redirects, require TLS verification, and reject 3xx before state/receipt persistence. |
| A3 rotation/replay | PASS (local) | Transfer scope binds destination, source ID, and Push key; captured context and cleanup rechecks reject rotation. |
| A5 alias/path traversal | PASS (Windows local) | Traversal, separator, symlink, hard-link, and real junction cases retain the source. POSIX descriptor races remain non-applicable to this platform run. |
| A6 single delete ownership | PASS (Windows local) | Thread and spawned-process source claims allow one terminal deletion; busy sources remain retained. |
| A10 mixed results | PASS (local) | Mixed retained/deleted outcomes and busy-source accounting reconcile totals. |
| A11 privacy | PASS (local) | Public/audit projection, smoke output, tracked source, artifacts, diff, and 200-commit history scans are redacted/clean under the stated synthetic test scope. |
| A4/A7/A8/A9/A12 | Not re-opened | Existing evidence only; this task did not reinterpret prior gates as a new completion claim. |

## Design And Merge Gate

- Design Gate: BLOCKED for full Phase 6 completion. The local implementation
  evidence is consistent with the sender/GenBox contracts, but the required
  isolated-execution and clean-owner-redeployment evidence is absent.
- Merge Gate: PASS for this sender-only candidate's local quality gate: the
  task branch contains only the reviewed regression/evidence commits;
  adversarial and full suites passed; syntax, whitespace, and secret scans
  passed. This is not a release or Phase 6 completion approval.

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

- Sender candidate: `079a29f` on `codex/phase6-security-gates-20260812`.
- This task adds regression/evidence-only changes atop verified candidate
  `9ad6570`; no production implementation behavior changed.
- Local quality evidence is PASS for A1/A2/A3/A5/A6/A10/A11; Docker/Linux and
  isolated execution remain external and are not represented as successful.
- Do not label Phase 6, Phase 7, deployment, cleanup execution, or release as
  complete based on this handoff.
