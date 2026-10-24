# ADR-001 — Single authority, detachable host integration and failure semantics

**Decision:** ACCEPTED as target design on 2026-10-24. **Implementation status:** NOT YET CONFORMANT.
**Scope:** psyshim/tools; overrides contrary proposed architecture text. Public wire schema remains controlled by `INTEGRATION_CONTRACT_V0_1.md` until replaced by a validated machine-readable release.
**Change governance:** amend this ADR only with an explicit counterexample, design rationale, updated tests and compatibility assessment.

## 0. Stop conditions and ownership

This project must not stop to ask the operator to choose among internal implementation details. Work on design, tests and mock independently; report results afterward. The receiving host team gets only the thin integration adapter and never unfinished core work.

There are **two distinct concurrency domains**: (1) multiple developers/assistants working in GitHub branches; (2) a Local Agent executing effects on one enrolled workspace. The first uses Git revisions/PRs. The second uses exactly **one owner per workspace**, serializing effects. Do not add a distributed multi-writer coordinator to solve a problem that does not exist. A VS Code human editing a file is an *external actor* not controlled by the lease, so genuine write races remain in scope.

## 1. Authority must be non-transitive

| Identity | May receive | Cannot acquire through protocol |
| --- | --- | --- |
| Browser | safe task views, artifact preview, approval UI | credentials for device, filesystem paths, generic tools |
| Host adapter | user/session-derived task forwarding, scoped upload handles | arbitrary tool requests, raw shell, Git, device admin token |
| Coding Service | task intent + policy-limited device operation descriptor | filesystem handle, workspace root, unrestricted local code execution |
| Device runtime | locally registered workspace capabilities after OS lease | host DB, user session cookies, model API credentials |
| Operation adapter | only narrow file/test/Git capability passed by device runtime | mint grants, bypass journal, select another workspace |

Authorizations intersect at each boundary; they are not unioned. `effective_grant = host_actor_acl ∩ service_task_scope ∩ device_workspace_policy ∩ operation_capability`. Untrusted JSON fields cannot widen those sets. Revocation or missing evidence means deny. No implicit authorization from possessing a task ID.

## 2. One state writer per domain

| Durable record | Only writer | Source of truth | Forbidden duplicate |
| --- | --- | --- | --- |
| Conversation, user and attachment ownership | Host backend | existing host datastore | agent shadow-user DB |
| Task lifecycle and event sequence | Coding Service | task DB + append-only events | host's second task state machine |
| Workspace enrollment + device grants | Device runtime | device-local registry | service-supplied absolute root |
| Operation claim, effect evidence and recovery | Device runtime | device-local journal | per-adapter independent journals |
| Files and Git worktree | Device runtime's adapter acting under lease | actual filesystem/Git | host filesystem edits |

The host MAY persist delivery cursors/task-to-conversation mapping. Those records are **not** task state. The service may cache evidence but never declare a local effect committed without a device receipt.

## 3. Process and packaging constraints

```text
existing-host --(host-client; HTTP only)--> coding-service
                                               |
                                      signed narrow device request
                                               |
                                               v
                                      device-runtime bootstrap
                                        + exclusive owner
                                        + journal / recovery
                                               |
                                     bounded adapter capabilities
                                        read/edit/test/git/browser
```

The host-client package depends only on protocol DTOs and HTTP transport. Installing it must not install the device runtime, model SDK or shell runner. The service must boot without the host framework. The device must boot offline without the host DB.

**One and only one composition root** constructs mutable file/test/Git adapters; stand-alone experimental classes are private and not network-reachable. Test harnesses use disposable workspaces. A static import rule supplements but never replaces process/package isolation.

**Local executor lock and journal live in an app-private state directory outside the repository** (keyed by canonical workspace identity, secure permissions), not as `.coding-agent-owner.lock` inside the user worktree. This prevents an agent-generated untracked file, repository modification on startup, interference with Git status and accidental publication. Canonical identity must address aliases/symlinks to the same root; avoid using user-submitted arbitrary paths as the identity. OS-backed lock covers *agent processes only*, never VS Code.

## 4. Write policy for uncoordinated editors

Do not claim that a SHA check followed by `os.replace` is safe compare-and-swap. There is a gap in which VS Code may save changes.

Decision for initial delivery: **unattended writes are DISABLED** unless a tested, platform-specific write protocol prevents silent overwrite, or an explicit device-local opt-in accepts the remaining conflict risk and requires fresh per-operation authorization. Approval cannot turn a best-effort check into atomic safety.

Safe initial read/propose mode reads from an immutable revision/snapshot when practical, or reports the exact observed hash. Applying a patch needs: canonical path/file identity checks, symlink/hardlink policy, preimage digest, authorized operation ID, journal claim, supported atomicity guarantee, postimage verification, and recovery evidence. When the guarantee is unavailable, return `UNSUPPORTED_CONCURRENT_EDIT`; do not silently fall back to a vulnerable writer.

Do not resolve this by imposing a lock that VS Code does not honor. Moving `os.replace` into a single local process only solves duplicate *agent* writers.

## 5. Operation delivery and recovery

States are intentionally split into **task status** (service-owned) and **effect status** (device-owned). A task cannot publish `COMPLETED` for a requested mutation until its effect has an authenticated terminal receipt.

```text
task proposal -> scoped grant -> device PREPARED -> device CLAIMED
                                                 -> attempt effect
                                                 -> inspect evidence
                              /---------------------+-------------------\
                           VERIFIED             REJECTED           INDETERMINATE
                               |                    |                   |
                           SUCCEEDED            FAILED             UNKNOWN
                                                                      |
                                                              manual/verified
                                                              reconciliation
```

Required invariants:
- Task creation is idempotent under a stable, actor-scoped key and canonical request digest, including response-loss before acknowledgment.
- Device operation IDs are separate from task IDs, created before effects; a changed digest under the same ID is rejected.
- An operation can be claimed exactly once by the device journal. `UNKNOWN` never auto-reexecutes solely because a client retries.
- An observed postimage is evidence, but is **not always proof of causation**. Resolve `UNKNOWN` to `SUCCEEDED` only when receipt or unambiguous independently corroborated evidence exists.
- `PREPARED` after crash cannot be assumed unexecuted unless the executor protocol proves the transition order durably. The recovery classifier must be conservative.
- Git commit requires owned-path staging, review of pre-existing index entries, a persisted precommit plan and lookup-by-identity before retry. Push is a distinct opt-in effect.
- Cancellation is a request; persisted actual terminal outcome wins over a racing cancel.
- Missing verification, unreadable journal or incompatible schema blocks all mutation; read-only diagnostics remain available.

## 6. Mock fidelity and adversarial isolation

Mock endpoints are a *replacement for Coding Service*, not a fake host or Local Agent implementation. These three test roles must not share process state or internal modules.

Contract mock is driven by versioned protocol schema; scenario state keyed by **test instance, actor, workspace, task and operation**. Every failure has an injection edge before or after a durable boundary and a deterministic trace. Examples: invalid principal/workspace, forged grant, task dedupe collision, response lost after task creation, duplicate/out-of-order events, 410 cursor expiry, cancellation race, rejected device policy, lost response after file edit/commit, journal corruption, withheld network, secret-bearing output, client timeouts and artifact expiration.

Test-control operations use a separate control-plane identity and an isolated loopback port. Production service never registers the control-plane route. A scenario switch does not reset storage unless separately requested.

## 7. Exact adversarial sequence obligations

**S1 — duplicate task:** authenticated Host A sends request K; Service persists K→T; response lost; Host retries same K+body; receives T. Altered body→409. Host B's identity cannot access T.

**S2 — duplicate device effect:** device claims E, applies a mutation, process dies before reply; retry E returns UNKNOWN or verified receipt, never unconditionally applies again. A fresh E for the same file must independently verify preimage.

**S3 — VS Code races:** editor saves between agent's hash comparison and rename. Unless platform protocol proves no overwrite, agent must not perform unattended edit. Test must deterministically position save at the vulnerable boundary.

**S4 — service restart:** durable task/event journal recovers ordered cursors and terminal state, actor ACL rechecked; reconnect cannot skip missing events.

**S5 — device restart:** ownership lease is reacquired only after predecessor stops; unknown effects reconciled before capabilities advertise writes.

**S6 — cancellation after commit:** Git commit has a recorded SHA; cancellation cannot rewrite outcome as CANCELLED. No second commit on duplicate response loss.

**S7 — Haiku/second developer:** independent branch/PR changes can conflict through ordinary Git integration. They do not justify two device executors or cross-writer locks in the app.

**S8 — agent removed:** normal chat, login and upload work without agent service, SDK or device process; frontend displays only coding feature unavailable.

## 8. Design contradictions discovered in current prototype

These are **BLOCKING DESIGN DEBTS**, not new features:

1. `execution_owner.py` writes `.coding-agent-owner.lock` inside the workspace, contradicting the no-surprise-worktree and least-privilege objectives. Must migrate to private state directory.
2. `journaled_edit.py` composes a hash-check/replace writer that is still unsafe against arbitrary external VS Code saves. An OS lease among agents does not repair the race.
3. `journal.py` and `journaled_edit.py` are exposed as importable independent helpers. The target requires one privileged bootstrap and no separate network entrypoint.
4. `fault_service.py` is a partial test double, not a full v1 contract implementation; mock passing alone cannot establish interoperability.
5. The original integration contract and handoff documents report old `mock_backend.py` and “no journal” status. Current implemented *prototypes* should be tracked in generated release evidence, not copied as static evergreen claims.
6. A collection of static forbidden-import checks cannot establish a capability security boundary; require deploy-time separation and tests with removed privileges.

## 9. Design acceptance before further integration implementation

A proposal is APPROVED only when it includes: (a) immutable module/authority dependency graph, (b) sequence traces S1–S8 with expected failure states, (c) versioned wire schema, (d) permission capabilities and their denial behavior, (e) mock fault injection specification, (f) test runner and expected observations, (g) rollback/detachment proof, and (h) explicit unsupported paths.

The receiving host AI is **never** tasked with closing any gap above. It only maps existing authenticated chat, attachment grants and presentation to the supplied client after agent-side release gates pass.

No additional assistant approval checkpoint is needed for this internal design work. Ship tested cohesive design/code changes and send a concise after-action report. Keep handoff blocked until all mandatory gates are evidenced.
