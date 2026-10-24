# Integration Contract v0.1 — Normative Design (NOT IMPLEMENTED)

**Status:** PROPOSED / reviewed against repository code on 2026-10-24. This is the normative target contract for future host and agent implementations, not a description of available endpoints.

**Normative terminology:** MUST = required for interoperability/security; SHOULD = recommended; MAY = optional. Where an older design document differs, this document wins for the v0.1 proposal. Future changes require a decision record and contract-test update.

## 1. Current reality and precise gaps

| Capability | Implemented | Exposed to host | Evidence |
| --- | --- | --- | --- |
| Workspace list/read | Yes | Only Local Agent loopback | `coding_agent/workspace.py`, `coding_agent/service.py` |
| Mock relay of read calls | Yes | Development mock only | `coding_agent/mock_backend.py` |
| Experimental replace-by-hash | Yes (single-process helper) | No | `coding_agent/patching.py` |
| Experimental unique-text replacement, Python syntax check | Yes (local callable helper) | No | `coding_agent/workflow.py` |
| Task orchestration / event cursor / durable journal | No | No | planned |
| AI model / MCP engine / terminal / Git commit | No | No | planned |
| Authenticated cloud relay / remote VS Code control | No | No | planned |

**Existing gaps:** The prototype's second pre-write check does not give cross-process CAS; operation IDs are generated *after* edits and are not idempotency keys; Python syntax validation is a separate opt-in function, not automatic rollback. The mock has no user/workspace ACL and MUST NOT be internet-exposed. None of these capabilities is production-ready. Published acceptance claims require running tests against an actual checked-out revision, not merely uploading code.

## 2. Trust boundaries

A. Host browser -> host backend: existing session authentication, CSRF protections where applicable, user-to-task/workspace authorization.

B. Host wrapper -> Coding Agent Service: authenticated service identity over TLS; principal and authorized workspace conveyed through trusted service claims, not client-controlled JSON alone.

C. Coding Agent Service -> device Local Agent: separate device identity, workspace grants, operation grants and replay protection. Local policy MUST take precedence over cloud approvals.

No component silently escalates grants. Tokens MUST not be embedded in user-visible logs/events. The Local Agent MUST bind its listener to loopback when no authenticated relay is configured. All side-effect APIs MUST default OFF until journal, concurrency and approval tests pass.

## 3. Host-facing task request

`POST /v1/tasks` with `Idempotency-Key` (UUID or equivalent stable opaque key) and trusted service authentication.

Required fields and validation:

| Field | Type | Constraint |
| --- | --- | --- |
| `protocol` | string | exactly `1.0` until negotiation supports another major |
| `host.conversation_id` | string | opaque, 1–128 chars, no raw secrets |
| `host.message_id` | string | opaque, 1–128 chars |
| `principal.actor_id` | string | opaque; must match authenticated claims |
| `workspace_id` | string | opaque ID registered with authorized device; never an absolute path |
| `instruction` | string | UTF-8, nonempty, max 16 KiB serialized |
| `mode` | enum | `read`, `propose`, `execute`; device policy may narrow |
| `attachments` | array | up to 4 references; signed read grants, no raw bytes |
| `client_request_id` | string | unique stable correlation identifier |

Agent returns 202 with `task_id`, `state`, `event_cursor`. Persist mapping of (authenticated host identity, idempotency key) -> canonical request digest/task ID before starting work. Same key and same canonical content MUST return same task; same key/different content MUST return 409. No queued task may start until mapping is durable.

## 4. Events and recovery

Per-task event stream has an increasing, gap-free sequence of durable integer IDs (serialized as strings), issued in journal commit order. `GET /v1/tasks/{id}/events?after=17` yields strictly events with ID > 17, ordered; an empty response is not task success. Duplicate delivery is legal, and host MUST dedupe by (task_id, event_id). Never promise exactly-once transport. When retention has removed the requested cursor return `410 EVENT_CURSOR_EXPIRED` and a safe task snapshot; do not silently skip events.

Task states: `QUEUED`, `INVESTIGATING`, `PROPOSING`, `AWAITING_APPROVAL`, `APPLYING`, `VERIFYING`, `COMMITTING`, `NEEDS_RECONCILIATION`, `COMPLETED`, `FAILED`, `CANCELLED`. Terminal state is immutable. `COMPLETED` needs evidence for the specifically requested action; a syntax PASS does not prove a broader test suite passed. Cancel may race completion: return persisted actual state, never claim cancelled if commit already completed.

## 5. Mutating operations — proposed safety contract

Every command, edit and commit MUST have a caller-generated stable operation ID **before** side effects. Persist `PREPARED` with task, workspace identity, target, request digest, expected hashes, principal, approval and planned verification; then execute and persist final result/observed SHA. On timeout after a possible effect, mark `NEEDS_RECONCILIATION` and inspect filesystem/Git before deciding whether to retry. Reject a different request using an existing operation ID.

Edits MUST reject unexpected workspace revision or preimage hash. `os.replace` is atomic replacement, **not** atomic compare-and-swap with an editor. Until a trustworthy external-writer coordination / conflict policy is implemented and adversarially tested, unattended writes MUST NOT be enabled. Git staging MUST list only agent-owned paths and verify no unrelated staged changes; preexisting dirty workspace MUST be handled explicitly. Push is a separately authorized effect and defaults disabled.

Tests MUST record actual argv, cwd, tool version if available, exit code, start/finish times, status (`PASS`, `FAIL`, `TIMEOUT`, `UNKNOWN`) and bounded/redacted stdout/stderr. Running a command is not equivalent to a successful test. No general shell execution without allowlist, timeout and explicit device permission.

## 6. Host wrapper behavior

MUST remain a thin translator: existing authentication, workspace ACL lookup, attachment grant, `POST /v1/tasks`, event-to-chat streaming, decisions/control, feature flag and error mapping. MUST NOT import agent internals, create patches, choose Git commands or run workstation tests. A missing service must leave normal chat intact. For approvals, include the operation's full canonical fingerprint and expiry; a user-facing text approval without exact fingerprint MUST NOT authorize effect.

MUST validate `GET /v1/capabilities` on startup and on service version changes. If required `tasks`, `events`, `approval` capabilities are missing, disable unsupported workflows explicitly, not via optimistic fallback to read endpoints.

## 7. Conformance checks and exit criteria

| ID | Scenario | Expected |
| --- | --- | --- |
| C01 | Disable agent or stop service | Existing chat remains operational |
| C02 | Two distinct users with different workspace ACLs | Cross-user task/status/event/decision -> denied |
| C03 | Duplicate task delivery | One persisted task; reused key with altered payload -> 409 |
| C04 | Drop SSE connection after event 17 | Replay from 17, deduplicated rendering, no lost events |
| C05 | Expired cursor | 410 + snapshot, no silent missing events |
| C06 | Stale file hash or concurrent external edit | No silent overwrite; conflict reported |
| C07 | Duplicate operation after network response loss | Reconciles one effect, no duplicate commit |
| C08 | Forged/replayed/expired approval | No effect |
| C09 | Failed/timeout test | No PASS, with evidence of exit/timeout |
| C10 | Agent removed or protocol major incompatible | Host chat still works; coding feature disabled |
| C11 | Symlink/hardlink/alias to forbidden file | Fail closed and no mutation |
| C12 | Hostile repo text, HTML, logs, credentials | No execution/markup injection; secrets redacted |
| C13 | Attachments expired/oversized or wrong MIME | Reject without leaking URL or bytes |

A capability is DONE only if: implemented at both relevant sides or in a contract stub, local tests executed and results recorded, adversarial case passes, and a commit SHA is identified. Keep separate status columns `SPECIFIED`, `IMPLEMENTED`, `TESTED`, `INTEGRATED`.

## 8. Change governance

Protocol: breaking field/semantics changes require new major version or new capability; additive optional fields in same major are ignored safely by older wrappers. No ambiguous dual meaning. Every change to task schema or state machine MUST modify conformance fixture(s), host mock and wrapper documentation in the same change set. Maintain explicit decision records for unresolved choices. Implement one bounded work unit, run focused tests, then commit.

## 9. Open decisions (do not guess in host implementations)

- Where Coding Agent Service runs (host server vs separate trusted node), and whether relay is required.
- Actual user/workspace enrollment and revocation source.
- Attachment grant transport/expiration policy and retention.
- Cross-process edit coordination under Windows/VS Code and atomic recovery.
- Agent engine selection/model billing/secret custody.
- Persisted journal DB/runtime details and crash recovery clock semantics.

Until resolved, mock integration stays on loopback and read-only. Host implementers must not invent defaults that expand permissions.
