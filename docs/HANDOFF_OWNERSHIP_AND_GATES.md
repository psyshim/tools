# Integration ownership and release gate

**Status: binding project policy (2026-10-24).** Read with [INTEGRATION_CONTRACT_V0_1.md](INTEGRATION_CONTRACT_V0_1.md), [HOST_WRAPPER_DETAILED_DESIGN.md](HOST_WRAPPER_DETAILED_DESIGN.md) and [PLAYWRIGHT_E2E_DESIGN.md](PLAYWRIGHT_E2E_DESIGN.md). This document controls **work allocation and handoff readiness**. API details remain governed by the integration contract. Where older schedules suggest the host should implement unfinished agent features, this policy supersedes that suggestion.

## One rule

**The receiving backend team implements only a thin adapter to its existing chat/authentication/upload/streaming interfaces. All coding-agent functionality, mock agent backend, SDK, executable client, contract fixtures, process control, tests and evidence are supplied by this repository *before* handoff.**

Do not ask the receiving AI to implement or design the agent, local workspace access, browser automation, orchestration, MCP, Git operations, journal, E2E infrastructure or code fixes.

## Ownership / deliverables

| Deliverable | Owner | Before handoff? |
| --- | --- | --- |
| Complete versioned agent task HTTP API and OpenAPI or JSON Schema | tools repository | YES |
| Working service with durable task lifecycle and events | tools repository | YES |
| Local Agent reads/edits, bounded subprocess, Git and validation | tools repository | YES |
| Local privilege boundary, file conflict control, crash recovery, idempotency | tools repository | YES |
| Full contract-valid agent mock with deterministic fault injection | tools repository | YES |
| Installable host-neutral thin client SDK (optional supported languages) | tools repository | YES for selected host runtime |
| Host wrapper reference implementation / examples and protocol fixtures | tools repository | YES |
| Unit/integration/security/negative tests and runnable smoke script | tools repository | YES |
| Playwright E2E adapter | tools repository | MEDIUM priority; NOT a prerequisite for first read/edit integration unless UI acceptance explicitly demands it |
| Map existing authenticated host user to registered workspace ACL | host team | Integration only |
| Wire current chat message/image handles to SDK request | host team | Integration only |
| Forward status/events, feature flag, errors, stop/approval interactions | host team | Integration only |
| Verify existing chat remains functional when detached | host team | Integration only |

## Release gates: when it is permissible to request host integration

**Do not hand off until all mandatory gates are evidenced on a pinned commit SHA:**

1. **Contract frozen for first integration:** concrete machine-readable schema, field examples, error/status and version semantics; contract tests pass against both real service and fault-injectable mock. No speculative endpoints or hidden internal imports required.
2. **Vertical slice:** launch two independent processes (mock host and agent), send a user-like task, observe task status and ordered resumable events, run a permitted workspace operation in a disposable fixture, receive explicit result and evidence. Includes disconnect/error/retry tests.
3. **Safety:** workspace root enforcement, per-operation permissions, stale-hash conflict, symlink and alias attacks, idempotent side effects, no duplicate commit on lost response, cancellation, bounded resource execution and redaction pass adversarial tests. Writes/exec remain disabled if the relevant safety gate is not passed.
4. **Host kit:** install command, pinned dependency versions, tiny client/adapter example, environment template without secrets, fake host ACL/image-store implementations, deterministic test fixtures, a single smoke command, rollback instructions, capability matrix, expected outputs and troubleshooting.
5. **Detachment:** disabling the agent leaves mock host chat intact; no shared DB, host internal package, forced migration or required local agent process for normal chat.
6. **Verification:** all claims cite actual executed test names/results and pinned SHA. Mark unrun tests UNVERIFIED, incomplete capabilities UNSUPPORTED. No success claim based only on docs or GitHub upload.

A gate can be marked blocked; do not transfer that work to the host AI as an implicit requirement. The host receives a small integration ticket only when applicable gates are green.

## The receiving team's allowed implementation scope

Exactly one light boundary layer in its existing backend:

```text
chat route  -> auth/workspace check -> thin SDK/API adapter -> external Coding Agent Service
upload IDs -> authorized expiring references --------------------^
events     <- escaped bounded event mapping <--------------------|
```

Host may create a feature flag, a compact authenticated client, task-to-conversation mapping, event cursor storage and minimal UI hookup only where existing chat cannot present actionable approvals. The host MUST NOT introduce model prompts, tool selection, agent planner, terminal access, filesystem patch logic, VS Code control, browser driver, Git, MCP server, execution journal, agent worker or a new side-effect retry engine.

External service owns all of the above. Local Agent remains the **only** process authorized to touch workspace files and run tooling, subject to explicit local permission. Host wrapper MUST NOT import `coding_agent` modules. Host integration is a replaceable adapter, with no reverse dependency from tools to host code.

## Responsibility-crossing checks (automated where practical)

- Architecture/static scan fails on wrapper imports of `coding_agent` internals, shell/process/Git/Playwright/MCP/agent-runtime modules, workspace filesystem writes, model tool-loop dependencies.
- Integration test fails if host routes reach Local Agent directly instead of Coding Agent Service.
- Fake coding service verifies exact allowlisted outbound routes, credentials, workspace scope and stable idempotency key.
- Unavailable coding service leaves ordinary chat and uploads usable.
- Agent service has no dependency on host database, session classes, framework or frontend build.
- Authz bypass, cross-user task access, grant expiration, forged approval, replay, cursor discontinuity or synthetic PASS is a blocker.
- No host work may be needed to fix core agent behavior; problems are reported to the tools repository with reproducible fixture and protocol test.

## Minimal host handoff ticket (future, NOT ready today)

1. Enable the packaged client under a feature flag with agent URL and server-side credentials.
2. Plug in the existing `authorize(user, workspace_id)` and `create_attachment_grant(upload_handle)` callbacks.
3. Convert existing chat requests to the documented SDK call; map returned event objects into existing streaming/chat presentation.
4. Provide a legitimate user approval mechanism or leave side-effect capability disabled.
5. Run provided contract/smoke tests and existing chat tests. Report only host-specific failures, no agent redesign.

This ticket must be delivered with a pinned tested release, runnable samples and verified compatibility, not just Markdown specifications.

## Current readiness (2026-10-24)

**NOT READY FOR HANDOFF.** The repository currently has read-only workspace HTTP APIs, a basic read proxy mock, experimental local edit helpers, and specification documents. It does *not* yet have the task API, persistent events, journal, engine adapter, terminal/Git automation or contract-complete mock. Existing tests do not prove end-to-end host integration. Playwright is medium priority and only specified.

Before proceeding with host implementation, finish those agent-owned pieces and prove the gates. Work iteratively: investigate one cause -> edit -> focused test -> commit -> next unit; batch cohesive commits when possible.
