# Development-assistant requirements gap audit

This is an open acceptance backlog, not a declaration that the product is complete.
Tests of scripted tool calls do not establish model competence. Recheck this list whenever
an input format, transport, persistence layer or effect tool is added.

| Priority | Requirement and hostile case | Current evidence / remaining acceptance |
| --- | --- | --- |
| P0 | Malformed model JSON: truncation, duplicate keys, wrong type, nonfinite numbers, invalid Unicode, excessive size/depth | `test_json_corruption.py`: reject before approval/dispatch, give model an ERROR, allow corrected next turn. Never silently repair effect arguments. |
| P0 | Every JSON boundary follows the same policy | Shared strict UTF-8 object decoder now covers provider envelopes/arguments, service/device HTTP, device responses and configuration. `test_json_boundaries.py` exercises actual HTTP rejection, duplicate grants, invalid UTF-8, chunked limits and depth. OPEN: JSON stored internally (notebooks/recycle metadata) and host SDK policy alignment. |
| P0 | Lost responses and restart after an approved effect | `test_manager_interruption.py` and `test_device_effect_uncertainty.py`: stop after unknown effects, preserve uncertainty across cancellation/failure/restart, honor cancellation during device startup, retain an UNKNOWN claim after post-write exceptions or failed receipt persistence. OPEN: hard process kills after each individual copy/delete/commit effect. |
| P0 | Partial multi-file operation and concurrent human change | `test_multifile_failure_barriers.py`: rollback unchanged files and newly created parent directories; preserve human bytes; require reconciliation when rollback fails. OPEN: hard process death during batch writes. |
| P0 | Disk full, permission change and unavailable durable store | `test_durable_store_faults.py`: failed COMMIT rolls back both event and state and leaves the connection usable. `test_new_file_failure.py`: fsync failure publishes no partial destination. Device receipt failure remains UNKNOWN. OPEN: combined full-stack IO fault injection and parent-directory crash durability. |
| P0 | Untrusted repository content and secret handling | Path and environment/privacy tests exist. OPEN: real-model repository prompt injection, secrets inside ordinary source/output/diffs, credentials in URLs. Redacting local usernames is insufficient evidence. |
| P1 | Relevant validation rather than merely green tests | Stale-import fixture proves syntax PASS misses a broken caller. `test_test_evidence_barriers.py`: zero-test, all-skipped and failure-hidden-by-wrapper outcomes are distinct from PASS; generic zero-exit commands are COMMAND_EXIT_ONLY. OPEN: disabled assertions, stale artifacts, changed tests hiding regression, wrong cwd/interpreter. |
| P1 | Cross-file semantic changes | Navigation/batch tools exist. OPEN: import aliases, dynamic references, exported APIs, config and docs, types, generated consumers. Validate actual callers; do not equate symbol search with complete reference analysis. |
| P1 | Dependencies and reproducibility | OPEN: missing/locked dependencies, offline install, Python/Node version mismatch, monorepo roots and multiple test frameworks. Installation requires an explicit effect policy. |
| P1 | Cancellation and resource budgets across layers | Process tests exist. OPEN: blocked model callback, disconnected device, descendants surviving parent exit, exhausted disk/memory, large event history and concurrent task starvation. |
| P1 | Platform/filesystem variation | Encoding/path tests exist. OPEN: case-only rename, UNC/network volumes, reparse points without privileges, long names, read-only modes, file locks, mixed newlines and permissions/executable-bit preservation. |
| P1 | Recovery usable by the actual user | OPEN: surface exact affected paths, known/unknown effects and recovery instructions in UI; reconnect without duplicate approval; verify after browser/service/device restarts. |
| P1 | Git integration with ordinary repositories | Selected local commits have fixtures. Hooks/signing/custom filters and complex merge/rebase/remote operations need explicit support or refusal; never imply ordinary Git compatibility from selected-commit success. |
| P2 | Persistent logs and token usage on user machines | Separate durable usage ledger, scoped totals, unknown-use accounting, synced JSONL export including optional task metadata, preview-first pruning preserving totals/idempotency, and consistent live service-DB backup/verified restore to a new path. Restored unfinished work is held for reconciliation, not replayed. `test_state_backup.py`, `test_store_schema_migration.py`, `test_corrupt_state_records.py`, `test_restore_process_stack.py` add schema/record health, IO failure and process evidence. See LOCAL_OPERATIONS.md. OPEN: whole-device archive, automatic retention/size policy, spending caps, ledger archival, future schema migrations and provider invoice reconciliation. |

Completion rule: each supported requirement needs a normal case, refusal case,
failure/recovery case and observable assertion at the layer that owns the behavior.
An existing test filename is only a pointer until its assertions and current run are checked.

Evidence reports now fingerprint Python sources before/after a run. A shared-checkout
source change makes the overall report fail even if individual assertions pass; use
the changed-path list to identify which results need a stable rerun. Browser ChatGPT
review via webai was attempted with the repository URL and local sources, but its
server on port 9978 was unavailable; no external review approval is claimed.
