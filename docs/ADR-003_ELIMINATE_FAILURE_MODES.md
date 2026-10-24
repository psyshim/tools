# ADR-003 — Reduce the possible failure states by design

**Status:** ACCEPTED TARGET DESIGN, NOT IMPLEMENTED. 2026-10-24.
Supersedes additive workarounds, not the public wire specification. Read with [ADR-001](ADR-001_SINGLE_AUTHORITY.md) and [ADR-002](ADR-002_PROTOTYPE_EXIT_AND_PROOF.md).

## Design rule

Prefer **removing a capability or state transition** over adding another guard or retry. The final architecture must have fewer authorities, fewer constructible entrypoints, fewer duplicated records, and fewer ways to enter UNKNOWN. A test documents the guarantee but does not create it.

## Decision A — Separate analysis from mutation using immutable inputs

The default task path is read/propose on a pinned Git commit or immutable content snapshot. AI produces an *artifact describing the intended patch* (preimage digests, paths, resulting content, review summary). It never receives a writable filesystem handle.

Do not build an unattended live-worktree editor for the first release. Live VS Code human saves are fundamentally uncoordinated with the agent; a pre-write SHA + rename is not CAS. Initial application of a proposal requires a **device-local explicit apply action** under exclusive local executor ownership, after revalidating the entire declared preimage. If safe atomic conditional replacement cannot be proved on the target OS/filesystem, **apply is unavailable**, rather than running a best-effort writer. An approved proposal may instead be exported as a Git patch/branch for the user to apply through existing Git tooling. Export is not secretly a live worktree write.

This removes the write race from the default reasoning path rather than increasing locking complexity.

## Decision B — Precisely one effect dispatcher

Task service issues typed intents; only Device Runtime dispatches permitted effects. Device bootstrap constructs the sole dispatcher, its private durable journal and a workspace lease in an app-owned directory **outside** the worktree. Direct `Workflow`, `JournaledEdit`, `patching`, generic HTTP read/write or shell entrypoints are not production routes; remove or move to test-only modules at cutover.

Adapters cannot construct each other or mint permissions. Treat read, local apply, approved test profile, Git commit and Playwright (medium priority) as **distinct capability kinds**, independently advertised only when installed and permitted. No universal `execute(command)` escape hatch.

## Decision C — One truth per state, one-way receipts

Host stores chat/user/uploads and delivery cursor only. Service stores tasks/events/decisions only. Device stores workspace enrollment, operational rights, effect journal and receipts only. UI displays these; it never reconstructs a second execution state machine.

Task COMPLETED after side effect requires a verified device receipt referencing exact `operation_id`, request digest, workspace identity and actual evidence. Transport duplication is allowed; only durable effect claims and reconciliation prevent repeat execution. `UNKNOWN` never auto-retries; unresolved effects disable mutation on the relevant workspace, without disabling unrelated read-only tasks.

## Decision D — Narrower physical distributions

Release four separately installable products: `protocol` (data only), `host-client` (HTTP/DTO only), `coding-service` (task orchestration, no OS filesystem tool permissions), `device-agent` (local runtime and adapters). Fault mock is a fifth **test-only** distribution. The host may install only `host-client`; its dependency closure MUST NOT contain any device executor or agent planner.

No Python packages shared by importing implementation modules across processes. Interfaces cross HTTP/IPC using versioned schema only. Deployment enforces permissions: coding-service credentials cannot access device workspace; host-client lacks local runner dependencies and local device credentials; mock cannot be deployed with production capabilities.

## Decision E — Simplify the failure model

| Avoid by construction | Instead of |
| --- | --- |
| Mutating live workspace while generating solution | Locking every AI step |
| Two agents owning one workspace | Distributed ownership arbitration |
| Multiple operation journals | Reconciliation across journals |
| Mutable shared object between mock and service | Test service implementation split |
| Generic shell/Playwright entrypoint for host | Narrow typed local profiles |
| Host task engine / duplicate state | Host SDK + correlation only |
| Auto-retry after ambiguous effect | Durable UNKNOWN + explicit local reconciliation |
| Success inferred from stdout or UI snapshot | Evidence-backed terminal result |
| Mock claiming conformance alone | Same contract suite against production service |

## Migration decisions (remove before adding)

1. Make the read/propose path immutable/pinned and its contract executable.
2. Split installable packages and prove forbidden code cannot be imported from host package environment.
3. Remove live mutating endpoints or ensure capability is absent before any host integration.
4. Replace in-tree owner lock with private app-state ownership, created only by device bootstrap.
5. Make public task/event/mock protocol match exactly; remove direct host-to-device legacy relay.
6. Add a local apply capability only after proving real external-human-save behavior and crash evidence on supported platforms.
7. Add bounded test, Git and Playwright adapters independently, never as prerequisites for basic chat.

## Design review checklist — deletion pressure

Every proposal must answer: **Which existing state, dependency, constructor, pathway or failure mode does this remove?** If the answer is 'none' and it introduces new ownership or retries, reject pending architectural justification. Count active write entrypoints (target: ONE), journal owners (ONE per workspace), public host-agent API implementation (ONE), and host-side agent dependencies (ZERO).

The documented target is not a claim that prototypes are currently safe. Until cutover and real contract tests, implementation status stays BLOCK.
