# Coding Agent — Architecture and Responsibility Boundaries

Status: design proposal (2026-10-24). Implemented behavior is explicitly distinguished from planned behavior.

## 1. Goal and non-goals

Attach Codex/Claude-Code-like repository investigation, editing, targeted testing, review and committing to an existing chat application **without importing the host backend**. The existing frontend already supports chat and image attachments. The agent is a removable service, not a VS Code automation extension.

Non-goals: replacing the chat app, copying entire repositories to a cloud database, collecting every VS Code terminal's output, using GitHub as a real-time command relay, enabling arbitrary remote shell execution, promising parity with specific commercial products.

## 2. Processes and ownership

| Boundary | Owns | Must not own |
| --- | --- | --- |
| Host frontend | Existing chat/images, rendering messages/events; optional approval and diff UX | Shell commands, filesystem access, agent decision logic |
| Host backend wrapper (owned by host team) | User/session authentication, authorization, message routing, image grants, agent request correlation, safe presentation | Tool internals, task scheduling algorithms, direct filesystem mutations |
| Coding Agent Service (this repo, detachable) | Task/state machine, model/engine adapters, budgets, approvals, event stream, task journal, tool dispatch | Host DB schema, host auth implementation, host frontend |
| Local Agent (this repo, separate local process) | Allowed workspaces, path-safe reads/edits, subprocess ownership, Git, targeted tests, local operation journal | Host user database, model credentials, business logic of the chat |
| Optional relay | Device authentication and outbound agent connection, encrypted transport, delivery/acknowledgment | Permission to broaden local workspace roots |

Host calls only versioned HTTPS API; the coding service contacts the Local Agent over authenticated channel. Local Agent establishes outbound connections for remote access; no public PC port. Local process may use loopback HTTP for development. MCP is a *tool protocol adapter*, not the source of filesystem or execution authorization.

Deployment may keep this repository under the backend mono-repo while running as an independently versioned process. Removing the service must leave chat functional.

## 3. Dependencies and contracts

Dependency direction:
- host wrapper -> public agent HTTP contract (never host imports in agent)
- agent core -> abstract ToolPort, EnginePort, JournalPort, ApprovalPort
- local agent adapters implement ToolPort; Codex/other integrations implement EnginePort
- Local Agent never calls the host database directly
- Versioned schemas, capability discovery and contract tests are mandatory before interoperability is claimed

Keep domain types in a small protocol package; transport adapters translate HTTP/MCP/CLI, not business logic. Workspace identity is a server-issued opaque ID bound to a configured local root; never accept arbitrary host-supplied absolute roots.

## 4. API layering and implementation status

**Currently implemented on loopback Local Agent only:**
- `POST /v1/workspace/list`: `{"path":".","depth":1}`, max depth 3, max 500 entries; skip symlinks on list
- `POST /v1/workspace/read`: `{"path":"foo.py","start_line":1,"end_line":200}`, max 200 lines, max 1 MiB file, UTF-8 decoded response with SHA-256
- Bearer token from `CODING_AGENT_TOKEN` and loopback bind. These endpoints are *not* a cloud/public host integration interface.
- No generic task API, commits, terminal execution, patching, event delivery, journal or model adapter yet.

**Proposed Coding Agent Service API (NOT IMPLEMENTED):**
- `GET /v1/capabilities`: protocol version, supported engines, tools and permissions
- `POST /v1/tasks`: create task (idempotency key, conversation correlation, workspace ID, instruction, attachment references and requested access mode)
- `GET /v1/tasks/{task_id}`: status, current step and last persisted event cursor
- `GET /v1/tasks/{task_id}/events?after=<cursor>`: ordered resumable events via SSE (polling fallback); stable event IDs
- `POST /v1/tasks/{task_id}/decisions`: approve/reject an exact pending action and its fingerprint
- `POST /v1/tasks/{task_id}/control`: cancel/pause/resume with idempotent semantics

These are design endpoints, separate from today's `/v1/workspace/*`; do not route them to the current Local Agent blindly.

## 5. Execution transaction

Task lifecycle: QUEUED -> INVESTIGATING -> PROPOSING -> AWAITING_APPROVAL (when required) -> APPLYING -> VERIFYING -> COMMITTING -> COMPLETED. Any active stage -> FAILED/CANCELLED/NEEDS_RECONCILIATION as applicable. Each transition is journaled before external effects and reconciled by operation ID.

One work unit: identify one cause -> propose minimal patch -> verify preimage SHA-256 and workspace/revision -> apply -> targeted test -> review diff -> commit -> next unit.

Strong invariants:
- Do not overwrite user changes; reject hash mismatches and replan. Implement filesystem-local locking and atomic replacement, including cross-process conflict detection.
- Hash check and write must be in one guarded critical section; simple check-then-rename alone is insufficient against concurrent editors.
- Every side effect has stable operation ID and persisted PREPARED/EXECUTING/SUCCEEDED/UNKNOWN outcome. Retry UNKNOWN only after reconciling actual filesystem/Git state.
- Commit scope is an explicit allowlist of patch-owned paths. Never stage unrelated dirty files. No automatic push by default.
- Tests report command, environment identity, exit code, captured bounded logs, and actual status; never infer PASS from missing logs.
- Budget max calls, wall time, changed files and failed retries; explicit stop conditions and cancellation propagation.
- If a task disconnects, host can resume event viewing; actual continuation requires a running service/local agent and durable operation store.

## 6. Security model

Host authenticates end users and decides which user may address which workspace. Service authenticates host and per-device sessions; Local Agent enforces roots and operation permissions independently. Treat model output, repository files and command logs as untrusted. Shell executes only after policy checks; prefer argv-based command execution, fixed cwd, controlled env and timeout, not arbitrary shell strings. Use deny-by-default for execution, write, git push, network access and secret paths; host approval cannot elevate device's local policy.

Secret redaction before transmitting events/diffs; cap bytes, path count, line ranges and log retention. Image attachments use signed, short-lived, scoped references (not base64 blobs inside journal) with MIME/size checks. Bind approvals to task, operation, workspace, arguments, expected file hashes, expiration and requesting principal. Audit denied and accepted decisions.

## 7. Planned modules and phased delivery

`coding_agent/workspace.py` and `coding_agent/service.py` currently supply initial read-only Local Agent. Evolve toward:
- `protocol/`: versioned requests, errors, capability negotiation
- `core/`: task state machine, orchestrator, budgets, approvals, durable journal
- `local/`: workspace guard, CAS patching, process runner, Git driver, local journal
- `adapters/`: loopback HTTP, host-facing service API, MCP, model/engine connectors
- `tests/contracts/`: host stub and agent stub cross-version compatibility

Sequence: (1) formal contracts/host stub (2) safe patch CAS + targeted test (3) command ownership/logging and Git (4) orchestrator and journal (5) authenticated relay/reconnect (6) UI/browser/VS Code integration.

## 8. Acceptance and detachment tests

- Existing chat works with agent service absent, disabled, unreachable, or downgraded.
- Agent starts without host packages/config or host database and passes contract tests against fake host.
- Two hosts can integrate through identical public protocol without agent code changes.
- Invalid paths, symlinks, concurrent edits, duplicate side-effect requests and replayed approvals cannot escape policy.
- On network loss after a commit, recovery finds the committed SHA without making another commit.
- Explicitly distinguish planned acceptance tests from currently executed tests.

See [HOST_BACKEND_WRAPPER_REQUIREMENTS.md](HOST_BACKEND_WRAPPER_REQUIREMENTS.md) for requirements the host team can implement independently.
