# code_attach v0 — host wiring (1-day delivery)

**Status:** IMPLEMENTED with a runnable local browser host (`python -m examples.local_app`) and scripted-model integration tests. External chat-product integration and real-model execution remain unverified. See the root README for startup instructions.

## Deviation from ADR-004 (decided 2026-10-24, owner-approved)

To deliver edit + test in one day, v0 runs **in the host's FastAPI process on the same PC as the projects**. The service/device split, durable journal, signed claims and separate SDK package are deferred. Consequences:

- No crash recovery: live tasks and proposals are lost on restart. Local host/CLI activity logs persist independently; they do not resume execution.
- Task ownership is by host-supplied principal only; no device-side policy independent of the host.
- Apply and test run are protected by per-action user approval bound to a fingerprint (diff / argv), not by a separate device authority.
- Do not expose the router beyond the host's authenticated users.

## Layering (enforced by `tests/test_code_attach_boundaries.py`, import scan only)

| Layer | Modules | May | Must not |
| --- | --- | --- | --- |
| Host-facing | `router`, `providers`, `__init__` | HTTP translation, message-format conversion, call `Engine` | import `subprocess`, `os`, `pathlib`, `coding_agent`, tools/session, any model/network client |
| Agent | `engine`, `session`, `tools` | task registry, loop, tools; only `tools` uses `subprocess` and `coding_agent` | import FastAPI/pydantic or any model/network client |
| Host code | `complete`, `authorize`, `resolve_workspace` | the only place that talks to ChatGPT/Claude and decides who may open which folder | -- |

## Real-model smoke run

`examples/real_run.py` drives the Engine with any OpenAI-compatible endpoint (`OPENAI_API_KEY`, `MODEL`, optional `OPENAI_BASE_URL`), asking y/n on the terminal for every apply/test. NOT yet run against a real model.

## What the host writes (the whole adapter)

```python
from code_attach import make_router
from code_attach.providers import to_anthropic, from_anthropic   # or to_openai / from_openai

def complete(messages, tools):          # call the host's existing model client
    payload = to_anthropic(messages, tools)
    resp = host_claude_client.create(**payload)          # host's code
    return from_anthropic([b.model_dump() for b in resp.content])

def authorize(request):                 # host's existing session auth -> stable user id, else raise HTTPException(401)
    ...
def resolve_workspace(principal, workspace_id):   # host's ACL -> local folder path, or None
    ...

app.include_router(make_router(complete, authorize, resolve_workspace,
                               test_commands={"unit": ["python", "-m", "pytest", "-x", "-q"]}),
                   prefix="/api/code")
```

`test_commands` maps a name to a fixed argv. The model chooses only the name. Omit it to disable testing.

## Persistent activity logging (priority: medium)

The runnable local host and CLI enable durable, per-user JSONL activity logs by default, outside the workspace. Instructions, assistant output, tool results, approval requests and decisions, cancellation and final states are recorded with task IDs and UTC timestamps; every append is flushed and fsynced. Restarting leaves these files intact. The local app displays the exact directory and accepts `--log-dir` before `--test-command`.

An embedding host supplies `event_sink=ActivityLog(directory).record` to `make_router`, importing `ActivityLog` from `coding_agent.activity_log` in its own wiring code. The router itself performs no filesystem access. A logging failure before start/approval returns 503 without executing that action; worker logging failures produce FAILED. Logs may include user-supplied source/diffs and are not redacted source archives. Credentials/HTTP headers are not part of the event schema. Retention is local and unlimited until the owner removes files.

## HTTP surface (under the chosen prefix)

| Call | Purpose |
| --- | --- |
| `GET /availability` | `{available, modes}` |
| `POST /tasks` `{workspace_id, instruction, idempotency_key}` | start; same key + same body returns same task, different body → 409 |
| `GET /tasks/{id}/events?after=N` | poll; events `assistant`, `tool_result`, `approval_required`, `state`; `terminal` flag |
| `POST /tasks/{id}/decision` `{action_id, fingerprint, approve}` | answer an `approval_required` event; fingerprint must match |
| `POST /tasks/{id}/cancel` | cancel |

Frontend: poll events, render `assistant` text, and for `approval_required` show `data.summary` (diff or test argv) with Approve / Reject buttons posting back `action_id` and `fingerprint` unchanged.

## Behavior guarantees (tested)

- Edits are proposals; nothing is written before approval. A wrong fingerprint is refused (409).
- A stale file (changed since proposal) yields `CONFLICT`, not an overwrite.
- `..`, absolute paths, `.env`/key files are refused. Other users' tasks return 404.
- Tests report `PASS` / `FAIL` / `TIMEOUT` / `UNKNOWN` from the real exit code. Model errors end as `FAILED`, never `COMPLETED`.

## Known limits

- Approved test commands execute project code with the host process's privileges.
- No external-editor write coordination beyond hash check at apply time.
- Three symlink tests are explicitly skipped on Windows when symlink privilege is unavailable; path-escape rejection is still tested.
- The in-memory registry retains at most 64 tasks, including finished tasks. Restart the local host to reset it.
- Discovery tools: `find_files` (glob) and `search_text` (case-insensitive substring; max 5 hits per file, 50 total, skips `.git`, `node_modules`, `.venv`, `__pycache__`, binaries, files over 8 MiB; reports `more_in_file` and `skipped_large_files`). `start_line` continues a search beyond early matches. No regex, no symbol/AST search.
- Attachment read/edit supports UTF-8 files up to 8 MiB. Reads return at most 200 lines and reject pages over 32,000 characters. `start_line` alone selects the next page correctly. Each replacement is at most 20,000 characters per old/new string after CRLF normalization; complete approval diffs are at most 64,000 characters. Oversize edits/diffs are rejected at proposal time rather than after approval. The older read-only service remains at its default 1 MiB.
- `python -m examples.stress_check --output stress-report.json` runs an offline large-script acceptance matrix with fresh dummy projects, real regression commands and byte-level collateral checks. It does not use or grade a real LLM. It includes a complete Engine/approval flow on a 100,000-line script and separately approved helper-module extraction from a 10,000-line script. Multi-file edits are not transactional; the 20-turn limit still applies.
- Edits to CRLF files accept LF text and keep CRLF endings.
- Effects also include proposed file copy/move, recoverable deletion/restoration, directory creation and empty-directory removal. No destination overwrite or recursive tree deletion. Recovery storage is per-workspace and outside the workspace; see [ADVERSARIAL_ACCEPTANCE.md](ADVERSARIAL_ACCEPTANCE.md).
- Local Git status/diff/staged diff/log/branches and selected-file commit proposals are supported. Commits require an existing branch/history and no pre-existing staged changes, and pin HEAD/branch/index/source snapshots. Hooks, external filters and signing are disabled. Merge/rebase/cherry-pick/reset/clean/network operations are not exposed.
- UTF BOMs are detected; optional `encoding` supports explicit CP932/Shift-JIS and endian-specific Unicode. Strict round-trip and representability checks preserve original bytes or refuse the edit. Search reports encoding skips. A byte-preserving selected encoding does not prove a correct semantic interpretation of ambiguous text.
