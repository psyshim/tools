# Code Attach — local coding agent

An attachable FastAPI coding agent with a runnable local browser interface. It reads and searches a registered project, proposes edits, applies approved changes, and runs approved test commands. The host supplies its model callback, authentication, and workspace access policy.

## Run the local app

Python 3.11+, from this repository's root:

```powershell
python -m pip install ".[local,test]"
python -m examples.local_app --demo --workspace "$env:TEMP\code-attach-demo"
```

The demo directory must **not exist**. The app creates a tiny buggy project there; it never seeds an existing project. Open the URL printed in the terminal. Click **作業を開始**, inspect the diff, approve the edit, then approve the test. The model is scripted in demo mode; file changes and test execution are real.

For a real project and an OpenAI-compatible model endpoint:

```powershell
$env:OPENAI_API_KEY = 'your-api-key'
$env:MODEL = 'your-model-id'
# Optional: $env:OPENAI_BASE_URL = 'https://your-provider.example/v1'
python -m examples.local_app --workspace C:\path\to\project --test-command python -m unittest discover -s tests
```

`--test-command` must be last; its arguments are a fixed argv, without a shell. Omit it to disable test execution. Each edit and test run asks for approval in the browser. The CLI alternative is `python -m examples.real_run PROJECT_DIR "instruction" [test-argv...]`.

The server binds to `127.0.0.1:8767` (change with `--port`). Its printed URL contains a fresh session token in the fragment; the page removes it from the address bar and keeps it in tab session storage. Keep that URL private. Reload reconnects to an unfinished task in the same tab. Stop the server with Ctrl+C.

## Attach to an existing host

Production path: the host installs only the powerless `code-host-kit` wheel and writes a thin adapter ([docs/HOST_KIT.md](docs/HOST_KIT.md)); the Coding Service and the Device Agent run separately ([docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md)).

The in-process `make_router(...)` described in [ATTACH_V0.md](docs/ATTACH_V0.md) is a local prototype only and refuses to mount without `prototype=True`.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Tests cover edit approval, rejection, stale-file conflicts, owner isolation, discovery, real test subprocesses, the local host, and architecture boundaries. A scripted demo does not establish real-model quality or integration with an external chat product.

## Persistent local activity logs (medium priority)

For the detachable component's durable service, see [Local operations and token usage](docs/LOCAL_OPERATIONS.md).
It provides per-task/model/cumulative measured token totals, explicit unknown usage,
restart-safe accounting, JSONL export and preview-first retention commands. Local
installation data can live in this component's ignored `.code-attach/` directory;
the receiving host does not implement these management functions.

The browser app and CLI example save task instructions, workspace identity, assistant output, tool results, approval requests/decisions, cancellation requests, and final states as append-only UTF-8 JSONL, one file per task. Each event is flushed and synced before processing continues. On Windows the default is `%USERPROFILE%\.code-attach\logs\<workspace-hash>\<task-id>.jsonl` (avoiding Store Python's AppData virtualization); on other systems it is `$XDG_STATE_HOME/code-attach/logs/` or `~/.local/state/code-attach/logs/`, with the same workspace/task layout. The app prints the exact directory at startup and shows it in the footer. Use `--log-dir DIR` to override the browser app's directory.

Logs survive application restart and stay outside the project and Git. They contain source excerpts, diffs and user instructions; keep them private. Model credentials and authorization headers are not logged. No automatic deletion is performed. Existing hosts can opt into the same persistence with `make_router(..., event_sink=ActivityLog(directory).record)`. If saving a start or approval event fails, the action is refused; a worker-side logging failure ends the task as FAILED. Completed effects cannot be undone if recording their result fails.

## Offline checks for large scripts

```powershell
python -m examples.stress_check --output stress-report.json
```

No API key or model is needed. This creates fresh temporary dummy projects and checks actual source bytes, exit codes, and approval behavior. The JSON report identifies every case and measured duration; failed assertions produce a nonzero exit code. It also runs from the installed wheel. Use `--case engine_100000_lines_with_approvals` for one focused check.

To try a 100,000-line dummy script in the browser, without a model:

```powershell
python -m examples.local_app --demo --demo-lines 100000 --workspace "$env:TEMP\code-attach-large-demo"
```

This also requires a new directory. The scripted demo locates the function by search, reads its actual line range, and asks for edit/test approval in the usual UI. `--demo-lines` accepts 0–175,000 filler lines.

The matrix covers 100–175,000-line scripts, targeted tail edits, large CRLF files, extracting a helper module, duplicate-code disambiguation, search pagination, concurrent human edits, rejection without changes, exact size/edit boundaries, UTF-8 BOM, missing final newline, and unsupported encodings. The full Engine case exercises search → partial read → proposal → edit approval → apply → test approval → actual regression test. Approvals are automated only inside these generated fixture workspaces.

The attachment supports source files up to **8 MiB**, reading at most **200 lines / 32,000 characters** per call. Start-only reads work anywhere in the file. Search returns up to five matches per file; use `start_line` to reach subsequent matches. Files over the limit are skipped with `skipped_large_files` reported. Each replacement is limited to 20,000 characters for each of the old/new strings after CRLF normalization. Complete diffs up to 64,000 characters are shown; larger proposals are rejected before approval. Enormous single-line reads are refused without changing source. Function extraction uses separately approved changes, not an atomic multi-file transaction. The 20-turn agent budget still applies, so large files should be searched and read in relevant sections rather than scanned page by page. These checks prove tool behavior, not a model's ability to choose a correct refactor.

The older read-only HTTP foundation keeps its default 1 MiB limit; the 8 MiB setting applies to `code_attach`.

## Everyday operations and adversarial gates

```powershell
python -m examples.housekeeping_check --output housekeeping-report.json
python -m examples.adversarial_check --output adversarial-report.json
```

The first command drives actual Engine approvals through directory/file creation, binary-preserving copy/move, recoverable deletion/restoration, empty-directory removal, selected-file Git commit, and hostile encoding fixtures. The second runs the source checkout's complete test suite, producing per-test evidence and ten grouped gates. It requires the checkout's `tests/` directory; no model is required. Windows/Linux CI is configured to run these gates. Skips remain explicitly unverified.

File mutations and Git commits are proposals and share the existing approval gate. Deletion creates a persistent recovery backup at `%USERPROFILE%/.code-attach/recycle/<workspace-hash>/`; `list_file_backups` and `propose_restore_file` recover original bytes without overwriting an existing path. Creation, copy and moves also refuse existing destinations. Directory removal accepts empty directories only. Recovery storage can be configured through `make_router(..., recycle_dir=DIR)` and must be outside the workspace.

Text reads, searches, edits and new files accept an optional `encoding`. UTF-8/16/32 BOMs are detected automatically. Known CP932/Shift-JIS or BOM-less Unicode require an explicit encoding. Decode/encode is strict and must reproduce the original bytes; there is no guessed fallback or replacement-character conversion. Edits preserve BOM, endianness and newline bytes. Invalid, mixed/ambiguous, non-round-tripping data or an unrepresentable replacement is refused before approval. Search reports `skipped_encoding_files` instead of pretending every file was examined. Some mixed data can be valid under a selected encoding; byte preservation does not prove the text's semantic interpretation.

`git_inspect` covers local status, working/staged diff, recent log and branches. `propose_git_commit` handles explicitly selected individual files in a repository with existing history and an attached branch. It refuses existing staged work, checks HEAD/branch/index/file snapshots, and protects index/ref publication. Working-file bytes stay unchanged; Git's built-in EOL/encoding conversion is pinned in the proposal, so a changed conversion causes CONFLICT. Hooks, external filters and signing are disabled; commits use the explicit local `code-attach <code-attach@localhost>` identity. No arbitrary Git command, network operation, merge/rebase/cherry-pick, force/reset/clean or automatic conflict resolution is exposed. Unknown publication outcomes remain UNKNOWN.

See [ADVERSARIAL_ACCEPTANCE.md](docs/ADVERSARIAL_ACCEPTANCE.md) for each barrier, its invariant, and the remaining unsupported work.

## v0 limits

Running tasks are in memory; restarting stops them. Saved logs and file recovery backups remain, but do not resume tasks or prove the outcome of an interrupted effect. Approved tests run project code with the current user's privileges. Multi-file transactions, recursive directory operations, complex/network Git operations, crash recovery and independent device enforcement remain outside v0. These offline gates do not establish real-model reasoning or repository prompt-injection resistance. This local development app must not be exposed to the internet.

The separate `python -m coding_agent.service --workspace DIR --port 8765` is the older read-only HTTP foundation, authenticated with `CODING_AGENT_TOKEN`. Historical distributed-service designs in `docs/` are not claims that those features are implemented.
