# Adversarial Mock Coverage and Release Policy

**Status: active quality gate; 2026-10-24. This is a fault-injection test harness, NOT a production task service and NOT ready to hand off.**

The point of this repository is to deliver an **already-tested** integration package, not to give the host-side AI a partially built agent and ask it to find obvious missing cases. The host implements only a thin adapter when all mandatory gates are proven. Each declared case requires an executable test, actual result and pinned SHA; documentation alone does not count as passing.

## Layers of tests

1. Unit: bounded path operations, validation, stable hashing and state transitions.
2. Contract: request schema, response schema, error codes, idempotency and event cursors against **both** the deterministic mock and real task service.
3. Adversarial: unauthenticated requests, impersonation, cross-tenant replay, duplicate requests, timeout after effect, stale approval, malformed JSON, oversized bodies, concurrent cancellation, process crash and restart, invalid workspace, symlink/hardlink races, denial of service, redaction.
4. End-to-end: host stub -> service -> Local Agent -> disposable repo -> targeted test -> observed Git state -> event replay. Playwright browser E2E remains medium priority, separate from mandatory service E2E.
5. Compatibility: service absent, incompatible version, older client, removable package, two hosts with different internals.

## Fault profiles (deterministic seeds; failure never mistaken for success)

| Scenario | Expected client behavior | Status |
| --- | --- | --- |
| normal read task | create, status, ordered events | basic mock exists |
| invalid token / control token abuse | 401, no task created | basic mock test |
| same idempotency key + same request | same task; no duplicate | basic mock test |
| same key + changed request | 409, no second task | basic mock test |
| loss of first response after allocation | retry with same key resolves same task | focused regression test |
| invalid typed instruction/workspace ID | 400, no task | focused regression test |
| task reports FAILED | never emit fake PASS | basic mock test |
| service 503 | client graceful fallback | basic mock test |
| event replay with duplicate ID | client deduplicates | mock stimulus exists; host-client assertion pending |
| expired event cursor 410 | snapshot + gap indicator | mock stimulus exists; host-client assertion pending |
| user A/B cross-task access | deny before service and at service trust boundary | NOT IMPLEMENTED |
| concurrent requests/race under same key | exactly one task | NOT TESTED |
| journal restart and lost reply | reconstruct from durable state, no duplicate work | NOT IMPLEMENTED |
| stale/replayed approval | no mutation | NOT IMPLEMENTED |
| partial/truncated/invalid event stream | explicit error, no silent loss | NOT IMPLEMENTED |
| client 429, 500, timeout and connection refusal | bounded retry/fallback | NOT IMPLEMENTED |
| malformed/huge body and wrong HTTP method | controlled response, no crash | PARTIAL |
| file mutation concurrent with VS Code | never overwrite human changes | NOT PROVEN |
| secret-bearing logs and attachments | masked, scoped, expire | NOT IMPLEMENTED |
| cancellation race after commit | report actual committed status | NOT IMPLEMENTED |
| Git index dirty or duplicate commit | no unrelated staging or second commit | NOT IMPLEMENTED |
| hostile repository instruction (prompt injection) | no tool permission escalation | NOT IMPLEMENTED |
| Playwright missing binary, bad selector, timeout, unwanted URL | bounded failure; no host browser access | PRIORITY MEDIUM, NOT IMPLEMENTED |

The current scenario control route `/__test/scenario` MUST be bound only to loopback test runtime with a **distinct control secret**. It must not be part of public service API or available in production. Resets are destructive and are only acceptable in isolated per-test fixtures.

## Gate policy

A feature may be marked **DONE** only after relevant normal, boundary, negative, race/retry and recovery tests pass on the exact repository checkout that is released. Include both expected and observed outcomes, test commands, hashes and known omissions in the release record. Regression tests must fail against the unfixed defect when feasible. Recheck dependency changes before integration. Do not claim a mock fault scenario validates the behavior of a host client without running that client.

Until task journal, authorization, operation idempotency, file concurrency policy and approvals are complete, disable writes/terminal/Git via host. An unsuccessful/missing test is not a PASS. Never pass unresolved core work to host-side AI.

## Immediate implementation batches

- A: complete deterministic mock HTTP protocol (schema validation, multi-principal ACL fixture, scenario isolation, event pagination/replay, dropped replies, request size and concurrency, no network exposure), run adversarial suite.
- B: real task service implements same machine-readable contract; run same tests against real and mock.
- C: authenticated host SDK with no agent logic; run cross-user, fault injection and reconnect tests.
- D: persistent local operation journal and verified file/process/Git mutations in disposable workspaces; run concurrency and crash/restart suites.
- E: independent Playwright adapter, optional medium-priority gate.

Only after A-D pass is a first full read/edit/commit host handoff eligible. Shipping a read-only host integration earlier must be explicitly labeled a limited preview, not complete coding-agent integration.
