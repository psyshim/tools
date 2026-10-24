# Upstream migration playbook — Sonnet code attachment to detached architecture

**Status:** actionable migration plan, NOT proof of conformance.
**Audience:** maintainer of `sonnet-attach-boundaries` and integrators.
**Inputs:** [independent review](REVIEW_SONNET_ATTACH_INTEGRATION_2026-10-24.md), [ADR-001](ADR-001_SINGLE_AUTHORITY.md), [ADR-003](ADR-003_ELIMINATE_FAILURE_MODES.md), [ADR-004](ADR-004_THIN_HOST_SDK_BOUNDARY.md).
**Rule:** retain working feature code; change ownership and deployment **one seam at a time**. Each phase is independently revertible and testable. The existing chat host MUST NOT implement the agent as a shortcut.

## Immediate safety posture (Phase 0)

Do not advertise Sonnet's router as a production host integration. `docs/HOST_HANDOFF.md` currently tells the host to copy `code_attach/` and `coding_agent/` into the backend; that is a prototype demo path only. Direct FastAPI mounting and live edit, Git, tests and deletion must be treated as local-only experimental capabilities. Keep ordinary chat intact.

**Exit evidence:** docs and demos explicitly label prototype; mutation cannot be reached by default production configuration; all current tests still execute. Do not mark safe solely because there is an approval prompt.

## Phase 1 — Lock in existing behavior without changing implementation

Create conformance fixtures for accepted behavior of read/search/proposal, file encoding, bounded test results, Git planning, approvals and task events. Record exact commits and actual executed test output. Negative fixtures must cover missing auth, actor/workspace mismatch, path traversal/alias, secret redaction, stale hash, lost response, duplicate event and service absent. Mark a scenario UNSUPPORTED where the prototype genuinely cannot provide it.

**Touch only:** `tests/`, `testkit/`, protocol fixture documentation.
**Do not:** change tool implementations, copy code into the host, claim durability.
**Gate P1:** baseline tests reproducible; no false PASS and each case has a stable fixture. Preserve acceptance behavior for later extractions.

## Phase 2 — Separate the public transport contract from internals

Create one versioned protocol schema (DTO, event envelope, states, error codes, capabilities). Standardize task creation, read/propose completion, reconnect cursor and approval semantics. Add an independent mock consuming the same schema. Define `proposal_completed` versus `mutation_applied` as distinct outcomes.

**Touch:** `protocol/`, `testkit/mock/`, protocol tests.
**Do not:** import `code_attach.session`, `coding_agent.workflow` or local tool classes into contract DTO package; do not let a mock engine substitute for service conformance.
**Gate P2:** identical contract fixtures validate mock and a minimal real service; major version mismatch fails closed.

## Phase 3 — Extract host-client as a separately installable, powerless package

Move HTTP handling, request validation, stable retry IDs, event cursor/dedupe, capability check and typed error mapping into shipped `host_client`. The receiving host only provides existing identity, workspace ACL, upload grants and chat event callback. Do not ask the host to provide a model tool-calling callback or a test command argv.

**Touch:** `packages/host_client`, example fake host, packaging manifest.
**Do not:** copy `coding_agent` or `code_attach` into the host. No import of `subprocess`, Git, raw filesystem tools, Playwright, model provider SDK or device code.
**Gate P3:** install host-client into a fresh environment **without** device/agent runtime; fake host boot works with service unavailable; host can be detached without chat regression.

## Phase 4 — Put tool logic behind an actual device boundary

Retain Sonnet's file search, proposal/diff, encoding, Git planning and bounded process logic as candidate **device-only** adapters. Extract them from `code_attach.tools.ToolBox`, which currently imports `Workflow`, `FileOperations`, and `GitOperations` directly. Device owns workspace mapping, operation permission, app-private lock and journal. Service transmits typed intent only.

**Touch:** `services/device_agent`, internal adapters, temporary device integration harness.
**Do not:** run tools in FastAPI host process; accept remote absolute root or raw arbitrary shell; create a second local execution owner.
**Gate P4:** run service and device as separate OS processes; host installation has no filesystem execution dependency; malformed grants and workspace mismatch rejected by device.

## Phase 5 — Make task and event state durable in independent Coding Service

Sonnet's `code_attach.session` and `engine` currently use in-memory task state. Move task ID, idempotency mapping, task/event append log and terminal outcome ownership to service-owned persistent store. Keep model planning there; device receipts alone certify actual mutations.

**Touch:** `services/coding_service`, task and event protocol tests.
**Do not:** move task state machine into host; infer success from missing events; blindly retry unknown effects.
**Gate P5:** process kill/restart at task acceptance and event delivery; same task ID on stable retry, no event gap, cross-actor access denied.

## Phase 6 — Enable safe capabilities independently

**Read/propose first** on pinned revision or immutable snapshot. Live-worktree apply must remain unavailable until supported-platform evidence proves that a human VS Code save cannot be silently overwritten; approval alone is insufficient. Bounded test execution, Git commit, local apply and Playwright are distinct capability grants and test gates. Test code runs in a device-owned environment, not host privileges.

**Touch:** narrow device adapters and capability tests.
**Do not:** expose generic command execution or a universal mutation fallback; do not stage unrelated Git changes or auto-push.
**Gate P6:** deterministic race injection at file update boundary; kill-point journal recovery; staged-change separation; cancellation-after-commit receipt; no false PASS.

## Phase 7 — Remove obsolete entrances and release host kit

Remove old direct host/router imports and unguarded tool constructors, or quarantine them as explicitly nonproduction demos. Publish pinned client, executable mock, sample host adapter, install/removal instructions, exact test commands and SHA-tagged evidence.

**Gate P7:** host maintainer writes only a thin adapter and can remove coding agent entirely without affecting baseline chat/login/uploads. Any failure in agent behavior is fixed here, not delegated to the host.

## Small-batch workflow and ownership

- Each pull request changes **one dependency boundary**, its fixtures and docs; no big-bang rewrite.
- Before editing, compare the latest `main` and `sonnet-attach-boundaries`; the branch currently diverges. Rebase/merge with conflict review, never replace current `main` blindly.
- Keep Sonnet responsible for feature logic. Haiku's completed changes to owner lock / inventory / AST checks already exist in `main`; preserve them.
- Migration reviewer (this thread) owns contract/safety review and non-overlapping docs/CI tasks. No changes to Sonnet's feature source without targeted coordination.
- An independently testable phase may merge even while later phases remain BLOCK, provided the public capability matrix honestly reports unsupported behavior.

## PR acceptance template

**Boundary moved:** old owner -> new owner.
**Removed dependency/path:** exact import/route that is now impossible.
**Behavior retained:** named fixture(s) and test evidence.
**Adversarial proof:** named negative test and observed failure mode.
**Rollback:** how the phase can be reverted without data loss.
**Capability status:** SPECIFIED / IMPLEMENTED / TESTED / INTEGRATED.
**Exact revision:** base SHA, proposed SHA, executed test command and results.

A PR is BLOCK if it merely adds another wrapper while the unsafe host-to-local-tool path remains installable and reachable, or if documentation calls an unverified capability complete.
