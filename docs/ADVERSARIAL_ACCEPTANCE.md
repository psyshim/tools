# Adversarial acceptance gates

The target is dependable everyday development work. A successful demonstration is insufficient: wrong inputs, concurrent changes, failures and unsupported operations must produce the right observable outcome. These gates measure the tool/runtime foundation, not a real model's reasoning quality.

Run from the source checkout without model credentials:

```powershell
python -m examples.adversarial_check --output adversarial-report.json
```

The report includes every test ID, actual result and duration, grouped into the following gates. Skipped checks are recorded as unverified (`PARTIAL`), never converted to PASS. Gates overlap; their counts must not be summed as independent scenarios. A failed or uncovered gate gives a nonzero exit code. Installed wheels also contain the portable `examples.stress_check` and `examples.housekeeping_check` fixture runners.

| Barrier | Hostile condition | Required outcome |
| --- | --- | --- |
| Workspace scope | Traversal, private files, alternate streams, reserved names, symlink/reparse paths | Refuse before an effect; preserve unrelated bytes |
| Discovery | Deep files, duplicate names, vendor noise, truncated matches, very large sources | Reach the requested section; report skipped/limited coverage |
| Encoding | CP932/Shift-JIS, UTF BOMs, wrong endian, invalid/truncated sequences, mixed bytes, non-round-tripping aliases, unrepresentable replacement | Preserve encoding, BOM and unrelated bytes exactly, or refuse before approval |
| Editing | Duplicate snippets, CRLF expansion, large diffs, missing final newline, human edits after proposal | Require unique context; show complete diff; detect stale source |
| Multi-file recovery and reference coverage | Second write fails, human edits during rollback, renamed definition leaves an old import | Restore earlier writes when unchanged; preserve human changes and report NEEDS_RECONCILIATION otherwise; demonstrate that syntax PASS alone misses broken consumers |
| Approval and identity | Wrong actor, wrong fingerprint, repeated decisions, cancellation, declined actions | No unauthorized effect and no false completion |
| Housekeeping | Destination collision, binary data, source replacement, backup failure, directory becomes nonempty | No clobber; preserve source; persistent backup before deletion; recover byte-for-byte |
| Git | Mixed staged/unstaged work, HEAD/branch changes, index lock, reappearing deletion, hooks/filters, uncertain publication | Commit only selected, approved bytes; preserve others; report CONFLICT/UNKNOWN; no arbitrary Git command |
| Execution | Missing command, fail exit, huge output, secret environment, timeout, surviving child, step/capacity limit | Report actual exit/state; bound execution/output; stop owned work; do not invent PASS |
| Logs | Restart, full/unavailable disk, failed approval persistence | Retain existing records; refuse unrecorded approval; logging failure cannot prevent cancellation |
| Protocol and separation | Malformed requests/results, duplicate delivery, offline dependencies, architecture leakage | Bound and classify errors; keep host/tool responsibilities separate |
| JSON boundaries | Duplicate keys, invalid UTF-8/surrogates, truncated bodies/envelopes, nonfinite numbers, excessive depth/size, chunked oversize input | Reject before dispatch; keep shortened history valid JSON; return a correction error for invalid tool arguments; never silently repair effect arguments |

## Added capability scope

File effects use the existing immutable proposal and approval gate: create, copy, move/rename, recoverable file deletion, restore, directory creation, and empty-directory removal. Binary copies and restores are byte-exact. Recovery data is local at `%USERPROFILE%/.code-attach/recycle/<workspace-hash>/` by default; it survives new tool instances. Do not place a custom recovery directory inside the workspace. Directory trees are not recursively removed or moved. Multi-operation changes are not atomic.

BOM-based UTF-8/16/32 is detected. Known CP932/Shift-JIS and BOM-less endian-specific Unicode must be selected explicitly using `encoding`. There is no heuristic fallback or replacement-character decoding. Original-byte round-trip is required. New text files can explicitly select an encoding. A wrong selection that still round-trips cannot establish the semantic correctness of the displayed text; the tool does not claim to detect all possible mixed encodings.

Git investigation includes status, working/staged diffs, recent log and branches. Local commits require an existing branch with history, no staged changes, explicit individual paths, and an exact HEAD/branch/index/file snapshot. File mode and the expected blob after Git's built-in EOL/encoding conversion are pinned too, and changed conversion is a conflict. Commit objects and a temporary index are prepared separately; Git's index lock and compare-and-swap ref update guard publication. Working-file bytes are preserved. Hooks, external filters, and signing are disabled, and the local commit identity is `code-attach <code-attach@localhost>`. Custom filter transformations and ordinary hook execution are not provided. A lost/failed publication or index update is UNKNOWN and can retain `index.lock` for inspection. No automatic retry or force operation follows uncertainty.

## Remaining barriers

`tests/test_multifile_failure_barriers.py` injects a failure on the second batch write and concurrent human changes. Batch editing preflights all files and attempts rollback after failure; it is not an OS transaction or crash-safe atomic operation. A failed rollback must report `NEEDS_RECONCILIATION`, never claim that nothing changed. A separate fixture deliberately leaves a stale import after renaming a definition: syntax validation passes while executing the consumer fails, then passes after its reference is repaired. This verifies the fixture's independent oracle, not automatic reference discovery or model refactoring skill.

Merge, rebase, cherry-pick, force/reset/clean, remote operations, recursive tree deletion, a separate OS privilege sandbox, crash-resumable execution, and real-model prompt-injection resistance are not implemented by these gates. Unsupported actions must be refused; passing tool fixtures must not be presented as proof that those capabilities work. Complex Git operations require their own preview, conflict-resolution, abort/recovery and lost-response fixtures before being exposed.

Every new capability or observed defect must add a fixture with an observable invariant. CI is configured to run the full gates on Windows and Linux and retain the JSON evidence; remote CI success must be checked separately from a local run. Adding a capability without its negative/recovery cases is not sufficient.

Test execution evidence also distinguishes `NO_TESTS`, `ALL_SKIPPED`, and
`INCONSISTENT` (a wrapper exits zero while the runner reports failures). Ordinary
zero-exit commands without a recognized test summary retain `PASS` with
`verification=COMMAND_EXIT_ONLY`; this proves command exit only, not passing tests.
Source SHA-256 fingerprints and any changed paths are recorded in acceptance
reports; a changing source tree cannot produce an overall PASS.

Class/module fixture failures and skips are recorded even though unittest does
not call `startTest`/`stopTest` for them. Records identify their fixture scope and
target. An `expectedFailure` marker cannot convert a known broken requirement
into acceptance success, and an unexpected success remains unverified until its
stale marker is removed and it is rerun.

Protocol data budgets count serialized UTF-8 bytes, including JSON escaping.
Oversized approval cards are refused before becoming decidable, with a request to
split the proposal; an approval diff is never silently shortened. Derived display
diagnostics may be omitted with an explicit marker, while the actual exit/status
and bounded raw output remain available.
