# Host AI handoff — minimal implementation instructions

**Read first:** [INTEGRATION_CONTRACT_V0_1.md](INTEGRATION_CONTRACT_V0_1.md) (normative, proposed). Also see [HOST_BACKEND_WRAPPER_REQUIREMENTS.md](HOST_BACKEND_WRAPPER_REQUIREMENTS.md). This checklist is deliberately short for an assistant with limited project context.

## Your job (host backend only)

Create a small, removable wrapper inside the existing backend. You MUST NOT implement autonomous agents, file operations, terminal commands, Git, MCP server, or model tool loops inside the host backend. Do not change existing chat behavior when the coding feature is disabled.

**Do not assume /v1/tasks exists today.** The current agent repo has only a read-only Local Agent and a development mock. Until the task API is implemented, develop against a contract mock, not the live read endpoints.

## Required host adapter functions

1. `enabled() -> bool`: feature flag, false unless configured.
2. `authorize(user, workspace_id, task_id=None) -> bool`: existing host user/workspace ACL; deny by default.
3. `create_task(authenticated_user, conversation_id, message_id, instruction, attachment_handles, request_id)`: validate fields, authorize workspace, create short-lived read-only image grants, generate stable idempotency key, send task request.
4. `read_task(user, task_id)` and `stream_events(user, task_id, cursor)`: reauthorize, forward service response, dedupe events, persist cursor for reconnect.
5. `decide(user, task_id, operation_id, fingerprint, decision_id)`: reauthorize, require specific not-expired approval; never auto-approve.
6. `control(user, task_id, command, request_id)`: reauthorize and forward idempotent cancel/pause/resume.
7. `agent_health()`: capabilities/version compatibility; graceful fallback.

These are conceptual function signatures, not framework-specific API routes. Implement using existing host conventions.

## Tight resource budget for this integration

- No model calls in the wrapper.
- No repository scan or source caching in the wrapper.
- No polling tighter than once per second if SSE unavailable; backoff when disconnected.
- Bound in-memory event queue and task response sizes; backpressure rather than unbounded buffering.
- Attachment is a short-lived reference, not binary copied into chat JSON.
- Use existing chat auth, upload store, HTTP client and streaming infrastructure where possible.
- No new long-running worker is required in the host for the first read-only integration.

## Implementation order with gates

**Round A:** feature flag + capability probe + host offline fallback. Tests: disabled path and unavailable agent, existing chat unchanged.

**Round B:** user/workspace ACL + task create request to mock, stable idempotency key. Tests: cross-user 403, duplicate returns one task, changed payload 409.

**Round C:** task status + resumable events mapped into chat. Tests: disconnect/reconnect at event N, deduped rendering, 410 cursor expiration.

**Round D:** attachment grant proxy. Tests: image allowed, expired grant, wrong MIME, large body.

**Round E:** approval and cancel. Tests: invalid/stale/other-user decisions denied, cancel race reflects persisted outcome.

Each round: inspect only relevant host route/function and 100-200 lines nearby; identify one root cause; modify; focused test; commit. The host may stop after Round A if agent task API isn't yet deployed, but MUST NOT fake subsequent gates.

## Acceptance message required from host AI

Report: files touched, exact tests executed and outcomes, service protocol version detected, integration enabled/disabled, current capability matrix, unresolved contract gaps and commit SHA. Never claim real VS Code editing or Git commit capability unless an end-to-end test actually performed it.

## Prohibited shortcuts

- Browser talks directly to PC Local Agent.
- Wrapper embeds an arbitrary command executor.
- Client-controlled `workspace_id` or `actor_id` treated as verified.
- Retry with newly generated idempotency key after ambiguous timeout.
- Automatically approve filesystem writes or pushes.
- Convert 503/error/empty stream into a successful assistant answer.
- Enabling incomplete task API by routing it to `/v1/workspace/read`.
