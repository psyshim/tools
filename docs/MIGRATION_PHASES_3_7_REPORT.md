# Migration phases P3–P7 — report (PR acceptance template)

Continues `MIGRATION_PHASES_0_2_REPORT.md`. Each phase moves one physical boundary; useful feature code was moved, not rewritten.

## P3 — the host gets a powerless client
- **Boundary moved:** everything the host imports is now `host_client` + `protocol` (stdlib only), built as a reproducible wheel (`hostkit_build`).
- **Dependency removed (host side):** agent, tool, file, Git, subprocess, framework and model code. Asserted by installing the wheel into an empty directory and running with site-packages disabled.
- **Behavior retained:** start / poll / observe / cancel / decide; stable idempotency key per (conversation, message).
- **Adversarial proof:** `tests/test_host_client.py` (lost responses, duplicate and missing events, version mismatch, HTML 5xx, hostile text, principal/cursor misuse), `tests/test_sample_host.py` (a complete adapter against the real router).
- **Rollback:** `pip uninstall code-host-kit`; ordinary chat is unaffected.

## P4 — the Device Agent is the only local executor (playbook Phase 4)
- **Boundary moved:** workspace access, file/Git/test tools and the operation journal now live in `device_agent/`; the service holds no roots and no paths.
- **Dependency removed:** service → project files. The service tree has no `code_attach.tools` / `coding_agent`; the device tree has no service code (`tests/test_service_device_processes.py`, minimal source copies, run with `-S`).
- **Behavior retained:** read/propose tool set, matching, outline, detection, privacy scrubbing (events carry workspace-relative paths only).
- **Adversarial proof:** wrong/absent device token, non-loopback client, malformed grants (every problem listed), workspace mismatch, wire cannot name paths/roots/commands, hosts isolated from each other (`tests/test_device_agent.py`, `tests/test_service_device_processes.py`).
- **Rollback:** delete the device grants; nothing else holds authority.

## P5 — durable Coding Service owns tasks, events and the loop (playbook Phase 5)
- **Boundary moved:** `code_attach/session.py`, `wire.py`, `providers.py` → `coding_service/` (git-moved; old paths are aliases). New durable `TaskStore` (SQLite WAL, gap-free per-task event sequence), host credential + namespaced principal, config refused as a whole.
- **Dependency removed:** the service tree contains no tool, file, Git or test-runner code; it reaches projects only through `DeviceClient`.
- **Behavior retained:** loop, approvals, wire mapping, provider converters, activity notices.
- **Adversarial proof:** `tests/test_service_recovery.py` (hard kill, kill mid-task, kill right after acceptance, kills at arbitrary moments, repeated restarts, corrupt store, bad config).
- **Rollback:** the prototype in-process router is still importable (now with `prototype=True`).

## P6 — effects only by explicit grant, once, and honestly
- **Boundary moved:** `apply`, `git_commit`, `test` become separate device grants (and need `experimental_mutation`); the service flag alone cannot enable them.
- **Added safety:** approval fingerprint binding; journaled prepare→claim→execute→reconcile (UNKNOWN never retried); `NEEDS_RECONCILIATION` task state; Windows write guard; cancel-after-commit keeps the receipt; staged-file protection for commits; no false PASS from tests.
- **Adversarial proof:** `tests/test_experimental_mutation_stack.py` (approve/reject/wrong fingerprint/double decision, human save in between, file held open, false-pass check, service killed while a decision is pending, device killed at `after_claim` / `after_effect` / `after_record`), `tests/test_write_guard.py`, `tests/test_effect_semantics.py`, `tests/test_device_effect_uncertainty.py`.
- **Rollback:** remove the effect capabilities from the grants; back to read/propose.

## P7 — quarantine and handoff
- **Boundary moved:** `make_router` / `make_protocol_router` refuse to mount without `prototype=True`.
- **Docs:** `docs/HOST_KIT.md` (host maintainer, one page), `docs/OPERATOR_GUIDE.md` (service + device deployment, grants and config examples, restart behaviour, reconciliation runbook, known limits); `HOST_HANDOFF.md` marked superseded.
- **Evidence:** `examples/conformance_evidence.py` now records the contract, the shared conformance suite (mock, in-process real, and real separate processes over HTTP), host client, sample host, device, recovery, effects and write guard, with explicit `UNSUPPORTED` rows. CI is configured to run it on Linux (3.11/3.12) and on Windows, but **no hosted runner has executed it yet**: everything above was run locally on Windows / Python 3.13 only.
- **KPI:** items R32–R36 added to `kpi/scope.json`; levels are computed from evidence by `kpi/kpi.py`. Deferred items D01 (durable recovery) and D02 (service/device split) are now delivered as R32/R33 and were removed from the deferred list, so the denominators changed by design (v0 +5, deferred -2).

## Capability status (nothing is claimed beyond this)
| Capability | Status |
|---|---|
| Read/propose through service + device as separate processes | tested (stack, conformance) |
| Approved apply / test run / git commit | **experimental**, tested incl. crash points; off by default |
| Concurrent human save on Windows | guarded for open handles and in-window saves; rename-over savers are a documented residual |
| Concurrent human save on POSIX | stale-content check only; no guard |
| Real model quality (a small hosted model) | not measured here; scripted models only (`kpi/real_eval.py` exists for when a model is available) |
| Host UI integration in the real chat app | sample adapter only; not verified against the actual host |

## How to reproduce
```bash
python -m unittest discover -s tests -p "test_*.py"
python -m examples.conformance_evidence --output conformance-evidence.json
python -m kpi.kpi --write
```
The evidence file records the exact revision and whether tracked files were modified.
