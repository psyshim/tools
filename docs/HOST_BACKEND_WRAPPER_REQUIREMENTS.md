# Host Backend Wrapper — Requirements

Status: proposed future contract (not yet implemented). This document is for the team maintaining the existing chat backend. Related: [ARCHITECTURE.md](ARCHITECTURE.md).

## Purpose and boundary

Install a small adapter in the existing backend that forwards user-authorized coding requests to a separately deployable Coding Agent Service. No host database, model classes, frontend framework, or internal modules are imported by the agent. The host MUST NOT manipulate workstation files or execute shell commands. Disable the adapter without disrupting ordinary chat or image attachments.

## Host-side mandatory responsibilities

1. Preserve existing chat and image handling. Provide an independently switchable feature flag, OFF by default.
2. Authenticate the existing user and enforce authorization for each workspace and task. Client-provided workspace IDs and conversation IDs are not authorization.
3. Use server-side service credentials and TLS to call the agent. Never send service or local credentials to the browser.
4. Translate the existing chat message into an agent task request, preserving opaque conversation/message identifiers and a stable idempotency key.
5. Convert uploaded image handles into time-limited, read-only attachment grants with size/MIME validation. No permanent public URL or local absolute path.
6. Forward ordered task events into the existing streaming transport, maintaining event cursors for reconnect; bound and escape untrusted content.
7. Route stop, status, and (when permitted) approval actions, independently authorizing every action. Never auto-approve writes, commands, commits, pushes, or network requests.
8. Surface timeouts, unavailable service, test failures, and partial results distinctly. Do not claim a command succeeded without recorded evidence.
9. Enforce quotas, payload limits, secret redaction, retention policy, and audit correlation.
10. Let existing chat continue if this agent service is absent or broken.

The **agent** owns planning, models, budgets, tools, approvals' action fingerprints, patch conflict checks, process execution, Git, journal, reconciliation and task lifecycle. The **Local Agent** additionally enforces workstation permissions. MCP is optional transport, not authorization.

## Proposed API, version 1 (NOT available in the current implementation)

Host -> Coding Agent Service; bearer service authentication or stronger mutually verified credentials.

- `GET /v1/capabilities`: supported protocol version, tasks, available engines/tools and permission modes.
- `POST /v1/tasks`: accept instruction, workspace ID, host correlation, attachment references and mode. Require `Idempotency-Key`.
- `GET /v1/tasks/{task_id}`: current state and last event cursor.
- `GET /v1/tasks/{task_id}/events?after=<cursor>`: replayable task events via SSE; cursor-based polling fallback acceptable.
- `POST /v1/tasks/{task_id}/decisions`: allow/deny exact operation fingerprint with stable decision ID.
- `POST /v1/tasks/{task_id}/control`: pause/resume/cancel with stable request ID.

Example task creation:

```http
POST /v1/tasks
Authorization: Bearer <service-credential>
Idempotency-Key: 8fd2a1a5-69a4-4bfe-8eac-97c5e6b89b90
Content-Type: application/json
```

```json
{
  "protocol": "1.0",
  "host": {"conversation_id": "opaque-conversation", "message_id": "opaque-message"},
  "principal": {"actor_id": "opaque-actor"},
  "workspace_id": "registered-workspace",
  "instruction": "Locate the failing test and suggest a fix",
  "mode": "read",
  "attachments": [{"id": "upload-1", "mime": "image/png", "grant": "short-lived-reference"}],
  "client_request_id": "stable-request-id"
}
```

Proposed response (202): `{"task_id":"agent-task-id","state":"QUEUED","event_cursor":"0"}`.

Identical idempotency key + identical body returns same task; same key + different body returns 409. The service must validate trusted principal/workspace claims, not rely solely on these JSON fields.

Event envelope:
```json
{"id":"18","task_id":"agent-task-id","type":"test.result","timestamp":"2026-10-24T00:00:00Z","payload":{"status":"FAIL","exit_code":1},"truncated":false}
```

Supported event families: `task.started`, `progress.summary`, `tool.started`, `tool.completed`, `change.proposed`, `approval.required`, `test.result`, `commit.created`, `task.completed`, `task.failed`. Summaries are user-visible status descriptions, never private model reasoning.

Suggested error format: `{"error":{"code":"AGENT_UNAVAILABLE","message":"Agent unavailable","retryable":true},"request_id":"..."}`. Use 400 malformed request, 401 unauthenticated, 403 forbidden, 404 undisclosed task, 409 stale decision or idempotency collision, 413 oversized, 429 quota, 503 unavailable. Never retry side effects under a fresh idempotency key.

**Compatibility warning:** Today's Local Agent implements only loopback `POST /v1/workspace/list` and `POST /v1/workspace/read` with a bearer token. These are read-only developer endpoints, NOT the host-facing task API. A new agent service implementation and contract tests must exist before integration.

## Security / approval

Host must authorize each task, event subscription and control request by its actual session user. Device-side policy is a second, non-bypassable authorization layer. Approved action fingerprints must bind task ID, operation ID, path/hash/arguments, actor, workspace and expiry. Replayed/stale approvals fail closed. Browser cannot talk directly to Local Agent. Use an authenticated relay when workstation is remote; workstation opens outbound connection. Secrets, sensitive path names, log contents and attachment links must be redacted as required before sending to models, journals or browser.

## Required wrapper tests

- Feature OFF / agent unavailable: chat functions, explicit unavailable result.
- User B cannot read, cancel, approve or create against user A's workspace/task.
- Duplicate task delivery returns one task; altered same key conflicts.
- Disconnect after event 17 reconnects from cursor 17 with no missing or duplicate displayed events.
- Expired attachment / hostile HTML in tool log is rejected or escaped.
- Changed file hash, stale approval and repeated decision do not mutate workspace.
- Timeout/cancel does not produce false PASS, duplicate commit or unbounded retries.
- Removing agent requires no host DB migration and does not break chat.

Host deliverables: minimal routing adapter, configuration/feature flag, workspace authorization mapping, secure attachment proxy/grants, event-to-chat presentation, contract tests. Optional rich diff/approval UI comes later; until then disable remote side effects or require local approval.

API major version and capabilities must be negotiated. Missing required functionality fails closed. No backend-specific assumption may leak into the detachable service.
