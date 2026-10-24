# Host Backend Wrapper — Detailed Implementation Design

**Document ID:** HWR-001  
**Status:** specification for implementation, NOT implemented.  
**Normative dependency:** [INTEGRATION_CONTRACT_V0_1.md](INTEGRATION_CONTRACT_V0_1.md). If the two disagree, its normative protocol contract takes precedence.  
**Audience:** host backend team / coding assistant with no access to Coding Agent internals.  
**Acceptance gate:** passing host-side contract tests, NOT simply compiling this document.

## 1. What to build

Add a **removable, fail-closed, thin HTTP integration adapter** to the existing backend. Keep current chat and image upload functionality unchanged. The wrapper is responsible for translating already-authenticated host requests into calls to the independently deployed Coding Agent Service and translating events back into existing chat infrastructure.

**The Coding Agent Service task API is not yet implemented.** Develop host integration against a mock conforming to the proposed contract. Never send task requests to the current Local Agent's /v1/workspace/list or /v1/workspace/read endpoints.

Definition of removal: with feature flag OFF, service stopped, package removed or URL unset, the ordinary chat path works, without schema migration, filesystem changes, agent imports or startup failure.

## 2. Responsibility ownership and prohibition matrix

| Concern | Host frontend | Host wrapper | Coding Agent Service | Local Agent |
| --- | --- | --- | --- | --- |
| User login/session | UI | **Authorize using existing host identity** | Verify service claims | No host auth |
| Conversation and uploaded images | UI | **Own metadata, scoped grants** | Consume only granted refs | No host upload database |
| Workspace access policy | Display only | **User-to-opaque-ID ACL** | Validate trusted delegated scope | **Final local root/permission enforcement** |
| Chat request selection | UI | Route explicit coding action | Accept task | None |
| Model choice, prompt/tool loop | None | **Forbidden** | **Owner** | No model reasoning |
| Source discovery | None | **Forbidden** | Decide what to query | **Execute bounded read** |
| Patch generation | Display diff only | **Forbidden** | Propose change | Apply allowed change |
| Filesystem modification | None | **Forbidden** | Request approved operation | **Exclusive executor** |
| Process execution, tests, Git | None | **Forbidden** | Choose task/validate result | **Exclusive executor** |
| Task orchestration and budgets | Display status | **Forbidden** | **Owner** | Enforce local limits |
| Task events | Render | **Authorize, forward, dedupe** | **Persist/replay** | Supply tool evidence |
| Approval | User interaction | **Verify actor; forward exact decision** | **Validate fingerprint/expiry** | **Enforce local policy** |
| Local operation journal | None | **Forbidden** | Persist task journal | Persist side-effect record |
| Service availability | Render | **Fail gracefully** | Provide capabilities | Local health only |

**No responsibility crossing:**
- Host wrapper MUST NOT import `coding_agent.*`, read host filesystem for coding tasks, call subprocess/Git/VS Code, parse/edit patches, construct agent tool calls, host an MCP tool server, run an LLM, or maintain a second task state machine.
- Agent MUST NOT access host ORM/tables, session cookies, upload storage without grants, or the host web framework.
- Local Agent MUST NOT accept an arbitrary host-supplied absolute workspace root, or accept approval as authority beyond its own device permissions.
- User-facing frontend MUST NOT talk directly to Local Agent or hold service tokens.
- Wrappers MAY keep *delivery metadata* (task ID, event cursor, idempotency mapping), but MUST NOT copy the execution journal or make side-effect retry decisions.

## 3. Reference host module structure

Names are illustrative; adapt to the host language/framework, not its internals.

```text
integrations/coding_agent/
  config                  # feature flag, service URL, timeouts
  client                  # transport/authentication, typed errors
  authorization           # existing host user/workspace ACL adapter
  attachments             # short-lived read-only grants
  routing                 # chat-to-agent request translator
  events                  # cursor/dedupe, chat-safe event mapping
  approval                # actor check + exact-action decision forwarding
  persistence             # task ID mapping/cursor only
  contract_tests          # mock service / adversarial fixtures
```

Dependency rule: `routing -> (authorization, attachments, client, persistence)`; `events -> (authorization, client, persistence)`; `approval -> (authorization, client)`. None depend on agent internal packages. A single host-registered entrypoint decides enabled/disabled; core host chat must not depend on this adapter in order to boot.

## 4. Host-facing adapter ports (pseudotypes)

```text
CodingAgentIntegration:
  probe_capabilities() -> CapabilityResult
  create_task(authenticated_session, conversation_id, message_id,
              workspace_id, instruction, attachment_handles, client_request_id) -> TaskCreated | IntegrationError
  get_task(authenticated_session, task_id) -> TaskSnapshot | IntegrationError
  stream_events(authenticated_session, task_id, after_cursor) -> EventStream | IntegrationError
  decide(authenticated_session, task_id, operation_id,
         action_fingerprint, decision, decision_id) -> DecisionResult | IntegrationError
  control(authenticated_session, task_id, command, request_id) -> TaskSnapshot | IntegrationError
```

Validation order on **every** operation: verify host session -> map task to owner/tenant and registered workspace -> verify ACL -> capability availability -> construct trusted service claims -> call service -> redact/sanitize response -> map to chat. A task ID's existence MUST NOT grant permission. If no ACL source is available, fail closed; do not temporarily use a global workspace.

Every outbound call has bounded connect/read timeout, configurable from host; retries restricted to demonstrably safe reads and idempotent requests with *identical* key and body. A timeout after a mutating request is `UNKNOWN`, never automatic completion or retry under a new key.

## 5. Request and event mapping

Input mapping:

| Existing host input | Outbound proposed contract field | Rule |
| --- | --- | --- |
| Authenticated session | Trusted `principal.actor_id` | Obtain server-side, never request JSON |
| Conversation/message IDs | `host.conversation_id`, `host.message_id` | Opaque stable values |
| Host workspace selection | `workspace_id` | Resolve from user ACL/registry, no absolute paths |
| User text | `instruction` | Keep intent; limit 16 KiB UTF-8 serialized |
| Images already uploaded | `attachments[]` | <=4 grants, read-only + short-lived; validate MIME, size |
| Stable host delivery ID | `Idempotency-Key`, `client_request_id` | Persist before request; stable across retries |
| Explicit mode | `mode` | `read/propose/execute`, intersect with authorized privileges |

When no explicit coding intent or feature is disabled, leave the existing chat path unchanged. Never silently interpret image upload alone as authorization to edit files.

Outbound creation: `POST /v1/tasks` with `protocol: "1.0"`; `202` means *accepted*, not completed. Persist returned `task_id` and original correlation before notifying chat client. Same idempotency key with identical body reuses one task; changed body is 409.

Event mapping: service events carry `(task_id,id,type,timestamp,payload,truncated)`. Host stores last *delivered and acknowledged* event cursor per stream consumer or derives it from durable host event records; dedupe by `(task_id,id)`. Events with ID <= cursor must not render twice. Cursor expired (410) requires fresh snapshot and an explicit history gap indicator, never pretending continuity. Bound event text and escape HTML and untrusted Markdown. `task.completed` is distinct from HTTP 200 and an empty stream.

The wrapper forwards only approved event fields; it MUST NOT invent test PASS, commit SHA or progress when agent is offline. Unrecognized noncritical event kinds MAY be ignored with recorded warning; unknown mandatory protocol/capabilities MUST disable that path.

## 6. Approval and cancellation

Side-effect approvals MUST identify an exact immutable operation: `task_id`, `operation_id`, `action_fingerprint`, actor, workspace, expiry, plus explicit decision ID. Wrapper authorizes the actor and forwards unchanged; Agent Service verifies canonical fingerprint; Local Agent finally enforces device policy. No text such as "OK" or a general chat yes may authorize unrelated operations.

Host without dedicated approval UX MUST leave write/execute/commit/push capability inaccessible unless device-side local approval independently satisfies the contract. A cancellation request has a stable request ID; cancellation races with completion are resolved by the agent's persisted final state. Wrapper never kills an arbitrary PID.

## 7. Deploy and rollback requirements

- `CODING_AGENT_ENABLED=false` default. Service URL and credential stored server-side, excluded from browser bundles and ordinary logs.
- On startup with disabled flag: no network call to agent, no mandatory module import.
- On enabled but agent unavailable or protocol mismatch: expose coding feature unavailable, keep chat alive. Use capped backoff/circuit breaker; never overwhelm weak host infrastructure.
- Browser-to-localhost is prohibited. Remote workstation requires authenticated relay and agent-managed enrollment.
- Diagnostics: correlation ID, task ID, duration, status/error code, redacted counts; no raw code, auth headers, file contents or image bytes in default logs.
- Detachment test: disable adapter and run existing chat+attachment smoke tests; no schema/worker required for ordinary messages.

## 8. Required contract test cases (host-owned)

| Test ID | Input/setup | Required assertion |
| --- | --- | --- |
| H01 | Feature flag OFF | Zero agent requests; existing chat+uploads work |
| H02 | Agent refused/timeout/503 | Explicit unavailable; no chat failure or false success |
| H03 | Invalid/missing session | 401, zero agent requests |
| H04 | User B uses A's workspace/task/cursor | Denied before outgoing call (403/hidden 404) |
| H05 | Duplicate delivery same stable key/body | One task, same task ID |
| H06 | Same key, different payload | Conflict; no second task |
| H07 | Events 1..17 then disconnect and replay | Render once each, no missing events |
| H08 | Cursor retention expired | 410 handling, explicit gap + snapshot |
| H09 | Expired/wrong MIME/oversized image | No attachment grant forwarded |
| H10 | Forged, replayed or stale approval | No approval forwarded/effect |
| H11 | Cancellation races completed task | Show persisted actual completion, no false CANCELLED |
| H12 | Hostile log/HTML/secret string | Escaped, bounded, redacted |
| H13 | Unknown major protocol/missing capability | Disable only incompatible coding features |
| H14 | Uninstall adapter | Core host app startup and chat smoke PASS |
| H15 | Wrapper source audit | No model, filesystem, shell, Git, MCP or agent-internal import |
| H16 | Failed test event / incomplete evidence | Never display test PASS or fabricated commit |
| H17 | Slow event consumer | Bounded buffer/backpressure, no unbounded memory growth |

The agent team additionally owns service-side checks for workspace isolation, stale hashes, duplicate execution, device policy, commit idempotency and crash recovery. The host team does NOT duplicate those implementations. Both teams run versioned protocol fixtures, recording actual commands and results. Unit tests alone do not establish end-to-end integration.

## 9. Deliverable and review form

The host implementer MUST report: host files changed; mapping of each file to the responsibility table; test IDs executed with observed PASS/FAIL; host-specific ACL/attachment mechanism; protocol and capabilities returned; unresolved issues; commit SHA. Unsupported task endpoints must remain stubbed/disabled until actually implemented by the agent.

A pull request is **BLOCK** if it imports agent internals, contains code that executes a shell/Git or edits workstation files, weakens session/ACL checks, fabricates success, auto-approves effects, sends credentials to browser, or makes ordinary chat depend on service availability.

## 10. Current implementation compatibility notice

As of 2026-10-24, the repository has loopback read APIs and `coding_agent/mock_backend.py` routes `/mock/list` and `/mock/read`; these are not the v1 task service. The experimental `coding_agent/workflow.py` and `coding_agent/patching.py` cannot safely be activated as unattended cross-process VS Code file editors. This document defines *future integration requirements*, not verified current capability.
