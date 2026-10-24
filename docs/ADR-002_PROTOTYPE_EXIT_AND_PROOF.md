# ADR-002 — Migration from prototypes to one authoritative execution architecture

**Status:** ACCEPTED AS DESIGN; implementation NOT CONFORMANT. 2026-10-24.
**Precedence:** [ADR-001](ADR-001_SINGLE_AUTHORITY.md) owns authority and safety; [Integration Contract](INTEGRATION_CONTRACT_V0_1.md) owns wire behavior. This ADR owns migration order, removals, and proof. No claim of test PASS is made by this document.

## 1. Problem demonstrated by source inspection

The current prototype is **not** the target architecture even though many helpers individually exist:

| Concrete current path | Observable mismatch | Required disposition |
| --- | --- | --- |
| `coding_agent/execution_owner.py` | Writes `.coding-agent-owner.lock` *inside* enrolled workspace; ADR-001 demands app-private state outside worktree | Replace with device-private lock registry, canonical workspace identity, same-root alias tests; old implementation becomes obsolete |
| `coding_agent/edit_runtime.py` | Acquires that in-tree owner and accepts caller-provided journal location; public constructor permits independent execution roots | Make device bootstrap the sole creator; state location derived internally, never supplied by remote API |
| `coding_agent/workflow.py` | Exposes directly callable edit; SHA check then `os.replace` cannot defeat unsynchronized human save | Not exposed from any service endpoint; demote to private experimental adapter; unattended write capability FALSE |
| `coding_agent/journaled_edit.py` | Uses process-local `RLock`, journal claim and edit; serializes only cooperative calls, not VS Code | Keep fail-closed journal behavior as prototype; no promise of atomic CAS |
| `coding_agent/journal.py` | Database records effect states but lacks workspace identity, actor, approval, before/after evidence, schema migration and recovery classifier | Versioned device-private journal schema before enabling mutations |
| `coding_agent/fault_service.py` | Task mock in the agent package, actor-token mapping and global scenario, not an independent public-protocol distribution | Move to testkit executable; mock never imports private engine state; add scoped deterministic faults |
| `host_mock/server.py` | Old direct read proxy to local workspace endpoints, not a host SDK talking to Coding Agent Service | Preserve only as legacy test fixture, never host-side reference integration |
| `coding_agent/service.py` | Direct loopback workspace read API without device principal/workspace enrollment mapping | Device-local diagnostic port only; never the public host API |
| `tests/test_architecture_boundaries.py` | AST import scan does not prove execution privilege isolation | Replace DONE claim with installed-package isolation and sandboxed process tests |
| `docs/INTEGRATION_CONTRACT_V0_1.md` | Current reality table still references moved mock and states journal absent | Keep wire schema, revise status matrix in dedicated tracked implementation inventory |

This mismatch table is the basis for removal-first migration; adding wrappers around each prototype would create parallel authorities and must be rejected.

## 2. Exactly one composition root

```text
device_agent.start(config)
  ├─ lookup locally enrolled workspace_id -> canonical existing root
  ├─ derive PRIVATE state path keyed by device-local workspace identity
  ├─ acquire exclusive OS-backed owner lease outside worktree
  ├─ open device journal and validate schema; refuse corrupt/incompatible DB
  ├─ inspect unknown effects; unresolved effects force read/propose-only
  ├─ construct ONE runtime / adapter registry
  └─ advertise capabilities derived from actual owner+policy+health
```

No `Workflow()`, `JournaledEdit()`, `OperationJournal()` or `SingleExecutor()` may be constructed from host request handlers, model tools or other independent composition roots. The public service holds **no** writable file handles. Device operation descriptors must be validated and translated by one dispatcher.

The OS lease protects against duplicate **device executors**, not external editor writes or multiple assistants preparing pull requests. A failed owner lease cannot fall back to an unlocked worker.

## 3. Minimal capability pipeline

Every capability is explicit and initially disabled:

| Capability | Precondition |
| --- | --- |
| list/read | device registered root + verified identity + path/secret policy |
| propose | list/read + task engine + bounded evidence |
| edit | propose + exact approval/grant + journal recovery + safe platform write protocol |
| test | device allowlisted argv/profile + process isolation and observability |
| git commit | verified edit receipts + clean ownership of staging paths + no unrelated index data |
| push | separate operation approval + remote allowlist |
| browser_e2e | installed Playwright adapter and isolated browser profile (medium priority) |

If a prerequisite is absent, omit capability entirely; never return an optimistic success and never route to an older unsafe helper as fallback.

## 4. Formal state / evidence boundaries

**Host owns:** actor login, conversation, image upload grants, task-to-message mapping and delivered cursor only.

**Coding Service owns:** task ID, plan, task/event transition sequence, approvals accepted from authenticated host, orchestration budget, service-side task idempotency; does not assert device effects without device receipts.

**Device owns:** local workspace registration, local policy, exclusive lease, operation ID claim, effect evidence, local journal and OS tools. The file/Git worktree is observed reality, not inferred merely from API responses.

Use a single decision table for each effect:

| Device journal | Observed evidence | Automatic action |
| --- | --- | --- |
| `PREPARED`, proved no execution started | untouched preimage | may proceed only through valid fresh grant and claim |
| `UNKNOWN`, authenticated exact durable receipt | receipt verified against operation+workspace | finalize result |
| `UNKNOWN`, postimage matches but no causal receipt | ambiguous | remain UNKNOWN; human/local reconciliation |
| `UNKNOWN`, preimage matches | effect may have been omitted or undone | remain UNKNOWN; never blindly rerun |
| `UNKNOWN`, unrelated changes | conflict | do not write; report human changes |
| `SUCCEEDED`, same operation ID/digest | immutable stored receipt | replay result without new effect |
| `SUCCEEDED`, different digest | collision/conflict | reject |
| journal corrupt / lease lost | no trustworthy authority | disable all mutations |

In all cases, device-reported `FAILED` is not automatically the same as task `FAILED`; the task may choose a different permitted plan after evidence review.

## 5. Work assignment boundaries with multiple AIs

Parallel development by this assistant and Haiku is permitted **only through disjoint branches/files or coordinated PR integration**. Do not treat GitHub contributors as concurrent device agents. Before each change, fetch latest relevant file SHA, edit only owned scope, require optimistic update against that SHA; if remote changed, re-read and reconcile rather than overwrite. A GitHub PR merge/commit is different from Local Agent Git commit automation.

Design and verification documentation may proceed while implementation is paused. No requirement for user approval between internal design iterations. Explicit user/device approvals remain mandatory for genuinely privileged or destructive effects; autonomy does not bypass security.

## 6. Architecture-level proofs (acceptance gates)

| Gate | Execution proof | Failure example that must be covered |
| --- | --- | --- |
| P01 package-only host | clean isolated host package install and import graph | imports subprocess, agent engine or device source |
| P02 absent agent | run host chat+image tests without installed agent runtime | chat fails to boot |
| P03 device singleton | same canonical root via alias/second process | second owner starts |
| P04 private runtime state | inspect clean Git status before/after device startup | lock/database appears as untracked file |
| P05 fail-closed mutations | capability probe with unsafe/unavailable writer | edit or Git still advertised |
| P06 identity chain | forge actor/workspace/operation ID over HTTP | accesses another tenant/workspace |
| P07 durable claims | duplicate request after effect with lost response | effect executes twice |
| P08 recovery | kill device at each persist/effect boundary | silent replay or fake PASS |
| P09 human VS Code save | deterministic save during compare/rename window | user edit silently overwritten |
| P10 test-command safety | malicious cwd/argv, timeout, orphan child | command escapes profile or hangs |
| P11 Git scope | unrelated staged/unstaged user changes | accidentally committed |
| P12 mock fidelity | run same published conformance fixtures against mock and actual service | mock PASS, actual service FAIL |
| P13 cancellation/approval | forged, late, duplicate decision or cancel after commit | unauthorized effect or wrong final task state |
| P14 host handoff | host AI only adds narrow SDK wiring | host needs to implement planner, Git or test runner |

Each gate records tested commit SHA, runnable command, observed evidence and explicit PASS/BLOCK; a Markdown assertion does not satisfy it. No gate may be waived merely because a corresponding unit test exists.

## 7. Migration milestones and stop rules

1. **Design freeze:** package DAG, workspace identity definition, private state directory derivation, capability policy, wire/error schemas and recovery semantics. Reject contradictory docs or code claims.
2. **Protocol first:** schema+SDK+service mock made independent installable units. Negative contract and cross-version tests green.
3. **Ownership extraction:** replace in-tree lock and arbitrary journal path; one device bootstrap; reject duplicate executors and aliases.
4. **Read-only real slice:** real service -> device read -> event -> host SDK. No mutating entrypoints.
5. **Mutation proof:** platform-specific editor-safe file updates, durable device receipts, kill-point recovery suite. Enable edit only where proven.
6. **Test and Git:** allowlisted process runner, scoped staging and repeat-safe commit evidence.
7. **Host handoff:** verified release, mock fault pack, SDK and minimal host adapter ticket.
8. **Browser E2E:** optional independent Playwright adapter; does not become host work.

**Do not label an integration DONE until its gate has a real observed PASS.** Current state: architecture decided, components remain prototypes, no host handoff.
