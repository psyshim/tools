# ADR-004 — Contract freeze and minimal host handoff

**Status:** ACCEPTED AS TARGET DESIGN; NOT IMPLEMENTED. 2026-10-24.
**Precedence:** ADR-001 authority, ADR-003 immutable proposal and restricted effects, this ADR for host integration surface. The older integration contract v0.1 remains historical proposed wire documentation where it conflicts with this ADR. A new machine-readable v1.0 contract must implement these decisions before release.

## 1. Design objective

A receiving AI must be able to integrate a completed, independently verified coding agent by wiring its existing chat/session/upload interfaces. It must not make design choices about tasks, file edits, Git, browser automation, retries, event semantics, or permissions. The receiving backend is intentionally unaware of device operation kinds.

To make that outcome structural rather than rhetorical, **ship a single host-client package** as the only dependency. The client exposes an `AgentGateway` with four lifecycle operations, plus optional approval forwarding. All protocol validation, HTTP request signing, cursors, deduplication, bounded retry, capability negotiation and error classification belong inside that package, supplied by this repository.

## 2. Minimal receiving-backend port

```text
HostAdapterPort:
  start_coding(user_session, conversation_id, message_id, workspace_selection, text, attachments)
      -> Submitted(task_handle)
  observe_coding(user_session, task_handle, resume_token)
      -> Iterator[SafeDisplayEvent]
  cancel_coding(user_session, task_handle)
      -> Accepted | CurrentTerminalOutcome
  availability()
      -> Available(capabilities) | Unavailable(reason)

  # Optional and disabled unless host already has a reliable dedicated decision UI:
  submit_decision(user_session, exact_action_handle, decision)
      -> Accepted | Rejected
```

`HostAdapterPort` above is the **host's tiny integration glue**, not a second API to develop from scratch. `AgentGateway` in the packaged SDK implements all protocol calls, event recovery, idempotency and typed error handling. Host must provide callbacks from existing systems:
- `identity(session) -> VerifiedPrincipal`
- `can_access_workspace(principal, workspace_id) -> bool`
- `grant_attachment(principal, upload_id) -> ExpiringReadGrant`
- `deliver_chat_event(conversation_id, SafeDisplayEvent)`

No host-persisted task state machine. The host may retain only an immutable task-handle/conversation mapping and a resume token as delivery metadata. The SDK cannot access the Host's tables except through these callbacks. If any callback is absent, that capability is unavailable.

## 3. Opaque handles and authority

The browser-visible task handle is a non-authorizing opaque reference. Every call reauthenticates the session and checks task/workspace membership. The agent SDK receives a principal from server-side identity callback and trusted service connection; it must refuse client-supplied principal fields.

Host callbacks are **not a substitute** for Service's signed claim verification and Device's independent root/operation policy. Grants always intersect. Devices cannot be named by absolute user-supplied paths. No raw terminal command, working directory, local filesystem path, raw Git command or Playwright script is a parameter of HostAdapterPort or AgentGateway.

Identity revocation blocks subsequent reads/decisions, including reconnection; task creation does not grant eternal access.

## 4. Capability negotiation replaces optimistic mode strings

Host's explicit coding action does **not** imply mutation. At initial release `read` and `propose` are the only modes advertised. The v0.1 proposed `execute` field must be rejected or omitted until a device-side safe apply, approval and receipt protocol passes real tests. Do not implement a fallback that routes to old workspace write or direct read APIs.

The receiving host obtains a normalized capabilities snapshot from the SDK, not a guessed feature matrix. An unavailable service leaves existing chat unchanged. Optional Playwright remains a device-owned capability, medium priority, never a host responsibility.

## 5. Exactly-once effects versus at-least-once messages

Only the Coding Service owns task dedupe, and only the Device owns effect dedupe. The SDK owns **transport retry policy**, with stable request IDs established before sending. The Host must not independently retry side-effect commands or fabricate task status.

Service events are durably sequenced. SDK emits `SafeDisplayEvent` deduplicated by `(task_id,event_id)`, with a visible gap marker on cursor expiry, and a verified status snapshot. An empty event stream, timeout or HTTP success is not a successful task. All event fields are size-capped; untrusted content is escaped at the rendering boundary.

A task's successful `propose` result is a proposed patch artifact, **not proof that files were edited**. The UI must label it accordingly. Completed execution requires a verified device effect receipt, an entirely separate capability.

## 6. Failure containment matrix

| Loss/failure | SDK behavior | Host behavior |
| --- | --- | --- |
| Agent not installed, disabled or down | Unavailable typed result | Original chat continues |
| Service timeout before task acknowledgment | query/retry identical stable key | retain pending message; no duplicate user task |
| Service accepts then drops response | recover same task via idempotency key | no second host task |
| Event stream breaks | reconnect after durable cursor | display gap marker if retention expired |
| Host session revoked mid-stream | auth callback denies | stop event delivery |
| Device unavailable | service reports task waiting/unavailable | no false PASS |
| Device rejects unsafe apply | explicit UNSUPPORTED_CONCURRENT_EDIT | no host-side workaround |
| Effect result UNKNOWN | device reconciliation only | no new operation ID on retry |
| Protocol major mismatch | coding unavailable | no legacy direct-device fallback |
| Cancellation races completion | display persisted final state | never synthesize CANCELLED |

## 7. What this decision deletes

- Host-authored HTTP endpoint schemas for every internal agent tool.
- Host-authored retries, event dedupe, task polling logic and recovery interpretation.
- Stand-alone host access to `/v1/workspace/*`.
- Host-side agent model/tool selection and prompt templates.
- Any notion that a Host AI must build Playwright, Git, process runner, or local file bridge.
- Public execute mode before device proof.
- Design ambiguity between completed *proposal* and completed *mutation*.

This is a **reduction** from the proposed `HOST_WRAPPER_DETAILED_DESIGN.md` structure: its `client`, `events` and most `persistence` responsibilities move inside the shipped SDK. It remains useful as threat model, but the final host handoff should be a single-page adapter recipe with ready-made SDK fixtures.

## 8. Design acceptance gates before implementation or handoff

1. SDK can install without coding service, Local Agent, filesystem tooling, model SDK or browser dependencies.
2. Two independent fake hosts can connect to the same mock with unchanged SDK code.
3. Host integration implementation has no task state machine, HTTP retries or event reconstruction algorithm.
4. Unauthorized workspace, forged principal, revocation after acceptance, expired image grant and leaked task handle fail closed.
5. Mock and real service satisfy identical published schema/error/event fixtures.
6. SDK handles response loss, duplicate events, cursor expiry, incompatible protocol and service absence without host custom logic.
7. Device offline or unsafe write condition results in unavailable capability, never direct local tool execution by Host.
8. Full delivery contains pinned SDK release, mock runner, negative fixtures, a host adapter sample, install/remove scripts, reproducible test commands and observed results.

**Current verdict: SPECIFIED, not IMPLEMENTED or TESTED.** The adapter should not be given to the receiving AI until each mandatory gate passes on the exact release SHA.
