# Local logs and token usage for the detachable component

This repository is the detachable component. Its service, device runtime and
administration tools own this information; the receiving host only uses the thin
client and displays protocol events. The host does not implement accounting,
log persistence, retention or recovery.

One service runtime may own a configured task database at a time. An OS-backed
lease is acquired before opening the database or loading the model. A second
runtime exits with code 5 before recovery; local usage/export/backup/prune remain
available. The sibling `<db>.service.lock` file must stay in place: do not delete
or replace it while a runtime is alive. A shutdown with a blocked model worker
retains ownership until that worker finishes and shutdown is retried, or the
process exits. This is a local filesystem lease, not a distributed/network-volume
coordination guarantee: a Windows network location or a hard-linked database is refused
at start (also exit code 5); keep the database on a local disk under one canonical path.

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

Full exports begin each task with a `task_metadata` record containing its original
instruction, principal, workspace and state from the same snapshot as the events.
Metadata-only exports omit those contents. The result counts event records and
task metadata records separately.

## Service database backup and verified restore

The service backup captures tasks, ordered logs, request identities and the usage
ledger with SQLite's backup API, including live WAL contents. It is not a copy of
just the main `.db` file. The snapshot is validated, synced and published without
overwriting an existing file; its SHA-256 is returned.

```powershell
$backupReceipt = python -m coding_service.manage --db .code-attach/service.db backup --output .code-attach/service-backup.db | ConvertFrom-Json
python -m coding_service.manage --db .code-attach/restored-service.db restore --input .code-attach/service-backup.db --sha256 $backupReceipt.sha256
```

Restore accepts only a matching hash, recognized backup format, supported schema
version/columns and healthy records. It writes a **new** database path; it cannot
overwrite a running or existing store. Optional `--timeout` sets a cooperative
deadline (1–300 seconds, default 60), checked during SQLite work, copying, record
validation and hashing. It cannot interrupt an OS call stuck in disk/network IO.
Failed checks or writes publish no destination.

All unfinished tasks in a restored snapshot become `NEEDS_RECONCILIATION` with an
explanatory notice in the same transaction. Even a task that had not started at
snapshot time may have incurred model calls or effects later. No old task resumes
automatically. Its idempotency key stays consumed; inspect the actual workspace,
device journal and usage before submitting a fresh request as a new message.
Historical completed tasks and measured usage remain in the snapshot. Adjust the
service configuration's `db_path` only after stopping the old service.

This command backs up **service state only**. It does not back up or restore the
device journal, file-recovery copies, workspace files, Git, configuration files or
environment credentials. It does not undo changes made since the snapshot. For
physical detachment, stop both processes and retain the whole configured device
state and component configuration as well; do not treat this service snapshot as
a complete machine/workspace backup.

The store uses a schema version marker. Known unversioned tables are upgraded
without discarding records. Unknown versions/columns and unrelated databases are
refused. Damaged durable JSON yields `LOG_UNAVAILABLE` or a failed local command,
never zero usage or a guess that no task had started. Recovery validates all
unfinished histories before starting any recovered worker.

The prototype `examples.local_app` still supports JSONL activity logs via
`--log-dir .code-attach/logs`. It records `model_usage` events, but the durable
service database is the accounting/reporting authority described above. Its
records survive service restarts. A copied script fixture proves persistence and
arithmetic, not that a live provider has returned correct billing information.
