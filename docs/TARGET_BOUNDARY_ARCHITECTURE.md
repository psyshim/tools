# Target architecture — executable boundaries before more features

Status: **DESIGN CANDIDATE / NOT IMPLEMENTED** (2026-10-24). This document refines physical ownership. Normative external payloads remain in [INTEGRATION_CONTRACT_V0_1.md](INTEGRATION_CONTRACT_V0_1.md). Earlier prose describing many independent workflow/patch entry points is historical prototype behavior, **not** the target public architecture.


**Decision reference:** [ADR-001 — Single authority](ADR-001_SINGLE_AUTHORITY.md) is the accepted authority/workspace/side-effect design; this document remains the migration target overview. If descriptions differ, ADR-001 controls architecture and the integration contract controls wire payloads.

## 1. Objective and design constraints

The eventual receiving AI writes **only** the thin adapter between its existing chat/auth/upload machinery and a fully packaged client. This repository delivers agent runtime, SDK, mock service with adversarial scenarios, schema, examples, integration harness, security tests, and operational proof **before** transfer. No host-side decision on agent internals, local filesystem, model tools, Git, browser driving, command execution, or crash recovery.

Primary invariant: the ordinary chat application boots and functions even when the entire coding-agent package/service is absent.

## 2. Process / authority graph

```text
[Browser, untrusted user inputs]
             |
             v
[Existing Host Backend: existing session, conversation, upload ACL]
             |
             v
[Host Adapter: calls packaged CLIENT CONTRACT only; no agent packages]
             |
       versioned HTTPS API + authenticated principal/workspace claims
             |
             v
[Coding Agent Service: task orchestration + approvals + event store]
             |
     authenticated *command intent* (never OS commands as privileges)
             |
             v
[Device Local Agent: registered workspace lease + exclusive operation owner]
             |
      private interfaces, permission checks and durable effect journal
             +---- ReadPort
             +---- EditPort
             +---- TestProcessPort
             +---- GitPort
             +---- BrowserE2EPort (medium priority)
```

There is exactly one local mutable-workspace operation owner per enrolled workspace. Multiple model assistants (including Haiku), browser tabs or service workers may propose tasks, but the Local Agent serializes all side effects on that workspace. Proposals are not execution leases. Remote agent service must not import local edit classes; only Local Agent may create them.

The Host Adapter is outside all local execution authority. Neither the Host nor Coding Agent Service receives raw workspace paths, file handles, shell capability tokens or device admin credentials. A service request contains scoped workspace IDs and approved operation descriptors only. The device may always refuse.

## 3. Physical package boundaries (target; staged migration)

```text
packages/
  agent_protocol/    # schema, typed DTOs/errors, protocol fixtures; no service imports
  host_client/       # single host-callable SDK; depends only on agent_protocol
services/
  coding_service/    # tasks, orchestration, event journal, approval state
  device_agent/      # single bootstrap / one runtime owner per workspace
    operation_owner/
    adapters/
      read/
      edit/
      process/
      git/
      browser/       # optional, explicit capability
testkit/
  mock_coding_service/  # same public protocol, deterministic injected faults
  fake_host/            # login/ACL/upload examples, no agent internals
  fixtures/             # adversarial scenarios, snapshots, test repos
tests/
  contracts/
  architecture/
  integration/
  adversarial/
```

Packages are distribution units, not merely subfolders. `host_client` installs without `coding_service` or `device_agent` as Python/runtime dependencies. `fake_host` uses `host_client` only. Mocks may implement the public protocol but cannot import private task engine data structures. Device-only components cannot import host frameworks, image upload DBs or model provider credentials.

Target dependencies form a DAG:
- Host implementation -> host_client -> agent_protocol
- Service -> agent_protocol + internal service ports
- Device -> agent_protocol + internal device ports
- Mock -> agent_protocol + independent fixture state
- Test harness -> only the packaged public entry points and temporary fixture credentials.

No reverse arrows; no import-time fallback into a different layer.

## 4. Single entry point and responsibility semantics

Only `device_agent.bootstrap` may create a mutable `WorkspaceRuntime`. On start: enroll an existing *local* root, acquire one OS-backed exclusive ownership lease, open journal outside editable root with hardened permissions, recover uncertain effects, then expose limited capabilities. A missing lease or unreconciled state means **no write/exec/commit capability**, not a second execution path.

The `WorkspaceRuntime` owns dispatch; adapters are given bounded capability handles rather than raw unrestricted paths. An external HTTP call may not instantiate `Workflow`, `JournaledEdit`, `SingleExecutor` or `OperationJournal` directly. Refactor experimental helpers into private implementation details and remove redundant public edit routes once the runtime is validated.

**Locking levels:**
- The device owner lease prevents accidental second Local Agent processes. It does **not** lock VS Code's editor. Do not claim otherwise.
- An operation is allocated one durable operation ID before effects.
- For VS Code/human edits, compare declared preimage revision immediately before write; user editing during a non-atomic compare-and-rename remains a risk. For uncoordinated editors, require an actual safe primitive or disable unattended writes. Do not invent cross-platform CAS.
- Haiku and this assistant may each work in their own branches and submit PRs, but must not simultaneously mutate the device workspace via separate owners. Git integration conflicts are handled by branch/PR review, not device-level distributed locks.

## 5. Effect lifecycle — a strict separation of intent, authority and evidence

```text
REQUESTED -> AUTHORIZED -> PREPARED -> CLAIMED -> (SIDE EFFECT)
                                            |        |
                                            v        v
                                     NEEDS_RECONCILIATION
                                            |
                      +---------------------+---------------------+
                      v                                           v
                   VERIFIED                                   NOT_APPLIED
                      |                                           |
                      v                                    deny/replan/new ID
                  SUCCEEDED

Explicit failure before any possible effect -> REJECTED
```

No event says PASS or completed based on merely claiming work. State transitions are durably written before the next irreversible action. After a crash, a claimed effect is never blindly replayed. Reconcile against actual file pre/post-image plus operation metadata, Git commit identifiers, process evidence or irreversible action receipts as appropriate.

`UNKNOWN` is a safety state rather than a job queue item. A daemon must not automatically change it to success simply because the file *happens* to match expected output: document required independent evidence and ambiguity handling for each effect type. Editing a file, running a test and committing Git are **distinct effects** with distinct operation IDs and evidence.

A single SQLite writer is an implementation option, not the architectural source of authority. The authority is the device execution owner; the journal is its durable witness.

## 6. Security / secrets by construction

- Do not expose a general command execution API or absolute-path API to host or service. A device-approved, versioned operation kind maps to a local allowlisted adapter.
- The Host SDK carries no local token and no workspace filesystem capability. All device credentials live on the device.
- Device roots are configured/enrolled locally. Server-selected arbitrary root strings are rejected.
- Mount tests/workspaces with least privilege. Prefer OS-level filesystem ACL/sandbox over denylist. Path exclusion is defense in depth; symlink/hardlink and directory replacement are subject to race-resistant handles and filesystem identity validation.
- Default agent-owned diagnostic artifacts remain device-side, bounded, redacted and accessible only through scoped expiring artifact handles.
- A compromised mock fixture or a repository README is never promoted into an operation grant.

## 7. Fault mock and deterministic verification architecture

The mock is a **separate replaceable service** speaking the exact public service contract, not a second task engine. Fault state is per test instance, actor, workspace and operation. Test-control API binds loopback only, requires separate control identity and cannot be enabled in production artifacts.

All fault profiles have clear injection points: before acceptance, after durable task acceptance but before response, during event delivery, after claimed local effect, during reconciliation, after successful Git commit but before acknowledgment, and after cancellation races. Test controls switch fault mode without unexpectedly deleting journal state. Reset is an explicit privileged fixture action.

Contract suite runs the same cases against mock and real service for success and normative failures. Separate adversarial mock tests confirm injection determinism. A working mock cannot substitute for real service conformance.

## 8. Failure isolation / recovery rules

- Host chat remains available when agent absent/unsupported/unreachable; failure does not affect host DB.
- Service restarts replay durable task events and never fabricate gaps.
- Device reconnect authenticates its device identity and advertises actual available capabilities.
- A local process crash releases ownership; the new runtime reconstructs journal and reconciles *before* accepting further mutating tasks.
- Failed, timed-out or cancelled test process reports observed exit state; no inferred PASS.
- Git commit after lost response is searched by durable evidence before new commit; no staging of unrelated user paths.

## 9. Non-negotiable design proof, before implementation

Before a subsystem is marked architecturally approved, produce:
1. An import/package graph demonstrating forbidden dependencies are **uninstallable**, not only lint-prohibited.
2. State-owner matrix: exactly one writer of each critical state record (host conversation, service task/events, device effect journal, file/Git worktree).
3. Authority graph: every privilege comes from a narrower grant; a forged JSON field cannot add rights.
4. Sequence diagrams for duplicate create, network loss after effect, service/device restart, concurrent human save, cancellation after commit and actor ACL revocation.
5. Machine-readable contract with versions and deterministic mock cases including adversarial inputs.
6. Proof of detachment with service absent, plus cross-version SDK tests.
7. A test schedule and runnable evidence capture on pinned revisions. Mark unsupported/unrun paths openly.

## 10. Migration order (design first)

D0 freeze this ownership and package DAG; eliminate ambiguous earlier proposals through an explicit architecture decision.
D1 write package manifests/public schema and architecture tests as *executable specification*; no new side effects.
D2 extract host client + independent service mock; prove two-process conformance and uninstallability.
D3 build device bootstrap and single authoritative runtime; use disposable fixture only.
D4 wire journaled edits behind runtime after demonstrated safe concurrent human-edit policy.
D5 bounded test/Git capabilities and recovery proof; only then hand off host kit.
D6 browser Playwright E2E adapter as medium priority, with no host changes.

**Existing code remains prototype, not evidence of adherence to this design.** Any new helper that creates an alternate side-effect entry path is rejected, even when its unit tests pass.
