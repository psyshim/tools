# Migration report — phases 0, 1 and 2

Follows the PR acceptance template of `UPSTREAM_MIGRATION_PLAYBOOK.md`. Status words are used strictly: SPECIFIED / IMPLEMENTED / TESTED / INTEGRATED.
Evidence is produced by `python -m examples.conformance_evidence` (records the revision and every test's own outcome); CI uploads it per run. Numbers below are from the working tree at the time of writing; the authoritative figures are in the uploaded report for the exact SHA.

## Phase 0 — immediate safety posture

- **Boundary moved:** "an approval prompt makes mutation acceptable" -> "mutation is off unless the host opts in explicitly".
- **Removed path:** `make_router(...)` with default arguments can no longer apply a change, run a command, commit or undo. Those tools are not shown to the model, are refused if called, and no approval card can exist. `/availability` reports `mutation: false`.
- **Behavior retained:** every earlier behaviour with `allow_mutation=True` (local demo and the tests that exercise approval and apply).
- **Adversarial proof:** `tests/test_code_attach_posture.py` (a model that tries anyway gets errors and no card; direct calls and forged decisions cannot mutate; the tree is byte-identical afterwards).
- **Rollback:** pass `allow_mutation=True`.
- **Capability status:** read/propose TESTED; mutation TESTED as a *local experiment only*, not safe against a concurrent editor save (see review B2, phase 6).
- **Labelling:** `HOST_HANDOFF.md` and `ATTACH_V0.md` now say PROTOTYPE / LOCAL-EXPERIMENTAL and must not be handed to the host team.

## Phase 1 — lock in behaviour with shared conformance scenarios

- **Boundary moved:** behaviour that used to be asserted only by implementation-specific unit tests is now asserted by transport-level scenarios that any implementation must satisfy.
- **Scenarios** (`tests/test_protocol_conformance.py`): capabilities, authentication on every route, valid/invalid request fixtures, malformed and oversized bodies, version mismatch on every route, proposal flow, gap-free and replayable cursor, bad cursors, idempotent retry and key conflict, actor and workspace isolation (indistinguishable from "absent"), odd task ids, approval card + wrong fingerprint + double decision, cancel idempotency, hostile paths and secret/absolute-path redaction, default no-mutation, receipt only after approval, stale file at decision time, every emitted event validates.
- **Honest gaps:** each world reports UNSUPPORTED (never a silent skip) where it cannot prove a scenario. Currently: the mock has no filesystem or mutation; the read/propose world has no approval stage; **service absent needs the host client (phase 3)** and is UNSUPPORTED everywhere.
- **Adversarial proof:** the fixtures contain refusals for every request kind and the scenarios assert the exact error code and HTTP status.
- **Capability status:** TESTED against the mock and two configurations of the real service on this Windows host; Linux/CI results come from the uploaded artifacts.

## Phase 2 — public transport contract, independent of internals

- **Boundary moved:** the wire shapes, states, error codes and event cursor now live in `protocol/` (stdlib only), not in router internals.
- **New:** `protocol/schema.py` (version, states, events, error codes, validators, `parse_cursor`), `protocol/fixtures/contract.json` (shared fixtures), `testkit/mock/` (executable mock, no shared code with the service), `testkit/transport.py` (same scenarios, two transports), `code_attach/wire.py` (internal events -> protocol envelopes, deterministic ids) and `code_attach/protocol_router.py` (the contract-shaped HTTP surface; the older `make_router` remains for the prototype demo).
- **Outcomes kept distinct:** `proposal_completed` (always `applied: false`; the validator refuses a proposal that claims otherwise) versus `mutation_applied` (receipt).
- **Removed dependency:** `protocol/` and `testkit/` may not import `code_attach`, `coding_agent`, FastAPI, subprocess, filesystem helpers or model SDKs (AST test in `tests/test_protocol_contract.py`).
- **Gate P2:** identical fixtures validate the mock and the real service; an other-major protocol version fails closed on every route. Both asserted by the conformance suite.
- **Rollback:** remove `protocol_router` from the host's mount; the demo router is untouched.
- **Capability status:** IMPLEMENTED and TESTED for read/propose; INTEGRATED: no.

## Not done (still BLOCK for production, per the review)

Phase 3 host client package without local tools; phase 4 device agent in a separate OS process; phase 5 durable task and event store with restart recovery; phase 6 mutation behind device-side receipts and race evidence; phase 7 removal of the prototype entrances. Until then the host handoff stays prototype-only.
