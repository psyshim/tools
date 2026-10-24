# Local logs and token usage for the detachable component

This repository is the detachable component. Its service, device runtime and
administration tools own this information; the receiving host only uses the thin
client and displays protocol events. The host does not implement accounting,
log persistence, retention or recovery.

For a source-based attachment, the receiving repository can ignore the entire
component directory, for example `/.external/code-attach/` in its own `.gitignore`.
That directory is this project (not an additional component). Its private state
then lives at `.external/code-attach/.code-attach/`. The receiving repository
tracks only its thin adapter/configuration reference; this project's code and
accumulated local state do not enter the receiving repository's commits. A wheel
installation and state outside the host checkout are also supported.

Place local development/installation state under this component's `.code-attach/`
directory (ignored by Git). Configure the service's absolute `db_path` as
`<component>/.code-attach/service.db`, and the device's `state_dir` and `lock_dir`
below `.code-attach/device` and `.code-attach/locks`. Device state must remain
outside every workspace granted for editing. If the component itself is a granted
workspace, choose an external state directory instead; do not weaken that boundary.
An absolute location outside the component is supported too. Detaching code need
not delete its state; retain this directory if the installation will be restored.

The durable service database stores task identity, ordered events, approval and
effect outcomes, and a separate model-usage ledger. `model_usage` notices carry
the same usage through the existing protocol. Each model attempt has a stable
task/call identity; repeated delivery does not add usage twice. Provider-reported
input, output and total tokens are stored, along with cached input and reasoning
output where provided. Cache/reasoning figures are subsets, not additional tokens.
Missing/invalid/partial usage is explicitly unavailable. A timeout may have consumed
billable tokens even when no count could be retrieved; no zero or estimated bill
is substituted. These are reported token measurements, not an invoice reconciliation
or currency estimate. Custom model callbacks can return a `usage` object with
`input_tokens`, `output_tokens`, `total_tokens`, `provider` and `model`.

The attempt is durably recorded **before** calling the model. If that write fails,
the call does not start. A crash or lost response leaves an unfinished/unavailable
ledger entry instead of silently omitting the attempt. A final usage receipt
updates that same entry once. `unfinished_calls` separates attempts without a final
receipt from completed calls whose provider omitted usage.

Run local management from the component checkout or installed package:

```powershell
# Cumulative totals, by model, with unavailable-call count
python -m coding_service.manage --db .code-attach/service.db usage

# A single task, or a principal/date window (timezone is required)
python -m coding_service.manage --db .code-attach/service.db usage --task-id TASK_ID
python -m coding_service.manage --db .code-attach/service.db usage --principal HOST:USER --since 2026-10-01T00:00:00+09:00
python -m coding_service.manage --db .code-attach/service.db usage --workspace shop --since 2026-10-01T00:00:00+09:00 --until 2026-11-01T00:00:00+09:00

# A stable JSONL snapshot. Existing export files are never overwritten.
python -m coding_service.manage --db .code-attach/service.db export --output logs-metadata.jsonl
python -m coding_service.manage --db .code-attach/service.db export --task-id TASK_ID --include-content --output task-full.jsonl

# Preview retention, then explicitly execute. Active tasks are excluded.
python -m coding_service.manage --db .code-attach/service.db prune --before 2026-10-01T00:00:00+09:00
python -m coding_service.manage --db .code-attach/service.db prune --before 2026-10-01T00:00:00+09:00 --execute
```

Exports omit event content unless `--include-content` is selected. Full exports can
contain source excerpts, diffs, outputs and local development information; keep
them in the component's ignored state directory when appropriate. Log pruning
clears old terminal-task instructions and removes events except the final state.
Task identities, request digests and the usage ledger remain, so retention neither
resets cumulative usage nor causes a repeated request to execute again. Pruning is
logical deletion in SQLite; it does not promise secure erasure or immediate file
shrinkage. There is no automatic retention schedule or provider-spending hard cap
yet; the local operator chooses what to export and prune.

Export writes are staged and synced before no-overwrite publication; a failed
write/fsync does not leave an apparently complete destination. Successful export
results include the SHA-256 of the exact JSONL bytes.

The prototype `examples.local_app` still supports JSONL activity logs via
`--log-dir .code-attach/logs`. It records `model_usage` events, but the durable
service database is the accounting/reporting authority described above. Its
records survive service restarts. A copied script fixture proves persistence and
arithmetic, not that a live provider has returned correct billing information.
