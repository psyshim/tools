# Host handoff — one page

Goal: the chat backend (FastAPI) gains "modify and test a local project" by writing **only the adapter below**. Everything else (tools, approval, test running, task loop) ships in `code_attach/`.

**You do not**: call any tool yourself, run commands, touch project files, write Git/test logic, add a task state machine, or add retries. If you feel the need to, stop: it belongs to `code_attach`.

## 1. Install

Copy the `code_attach/` and `coding_agent/` folders into your backend's import path (same directory as your app module, or anywhere on `sys.path`). Python 3.11+, `fastapi`, `pydantic`. Nothing else.

## 2. Write three functions (the adapter)

```python
from code_attach import make_router
from code_attach.providers import to_openai, from_openai      # or to_anthropic / from_anthropic

def complete(messages, tools):
    """One model turn using the call your backend ALREADY has.
    messages: neutral list (system/user/assistant/tool); tools: list of function schemas. Do not inspect or edit them.
    Convert in, call, convert out. code_attach never sees keys or endpoints."""
    payload = to_openai(messages, tools)      # -> {"messages": [...], "tools": [...]} in OpenAI chat format
    reply = your_model_call(payload["messages"], payload["tools"])   # must return the assistant MESSAGE dict
    return from_openai(reply)                 # content/tool_calls may be None; -> {"text": str, "tool_calls": [...]}

def authorize(request):
    """Your existing session auth. Return a stable user id (hashable); raise HTTPException(401) if not logged in.
    Called by every endpoint, so every endpoint is 401 without a session."""

def resolve_workspace(principal, workspace_id):
    """Your ACL. Return the local folder path this user may use for workspace_id, else None (-> 404)."""
```

If your model call does not accept tools, it cannot be used: the attachment is useless without tool calling. Anthropic-style hosts use `to_anthropic(messages, tools)` -> `{"system","messages","tools"}` and `from_anthropic(response_content_blocks)`.

## 3. Mount it

```python
app.include_router(
    make_router(complete, authorize, resolve_workspace,
                test_commands={"unit": ["python", "-m", "pytest", "-x", "-q"]}),
    prefix="/api/code")
```

- `test_commands`: name -> fixed argv, or a dict `{"argv": [...], "cwd": "services/api", "timeout": 900, "env": {...}, "description": "what it checks"}` for monorepos, slow suites and per-command settings (env values are never shown to the model or the approval card). The model sees the names and descriptions as a fixed list. Results include `parsed` (summary counts, failing tests with file and line, compiler/linter diagnostics) when the output is recognised. Run from the project folder unless `cwd` says otherwise. To pre-fill the table for a new project, review the output of `code_attach.detect.suggest_test_commands(workspace)`; it never runs anything. The **model chooses only the name**; you choose the argv. The tool must be installed in the backend's environment. Omit to disable testing. Test commands run with a **filtered environment** (PATH, temp/home, locale and similar only; host secrets such as API keys are not passed). Pass `test_env={"NAME": "value"}` to `make_router` for variables the tests really need.
- Optional persistent logs: `from code_attach.activity import ActivityLog` then `event_sink=ActivityLog(dir).record` (see `docs/ATTACH_V0.md`). Show people `display_path(dir)` (`~/...`), never the absolute path. If the log cannot be written, starting a task or answering an approval returns **503** and nothing is executed.
- If your app module defines `authorize`'s dependencies and also mounts the router, import them lazily inside the three functions (avoids a circular import).
- The backend must run on the machine that holds the projects. Never expose the prefix to unauthenticated users.

## 4. Frontend contract

`POST /api/code/tasks` `{workspace_id, instruction, idempotency_key}` -> **202** `{task_id, state}`. The task runs in a background thread; the POST returns immediately (state is usually `RUNNING`). Same key + same body returns the same task; same key + different body -> 409. Request bodies are limited to 64 KB (413), must be strict JSON of the documented shape (400 invalid JSON, 422 with `[{field, problem}]` and never an echo of the input). Authentication is checked first on every route, so unauthenticated callers always see the host's 401.

`GET /api/code/tasks/{id}/events?after=N` -> `{events, state, terminal}`. `after` is **how many events you have already received** (use the last event's `id` as an integer; ids are the strings "1","2",...). Poll about once a second; stop when `terminal` is true.

The model's tools (you never call them): navigation `list_dir`, `find_files`, `search_text`, `read_file`, `outline_file`, `find_symbol`, `project_overview`, `read_notebook`; edits `propose_edit`, `propose_replace_lines`, `propose_edits` (atomic multi-file), `propose_new_file`, `propose_notebook_cell`, `propose_undo`; file operations and local Git proposals; `apply_proposal` (needs approval); `run_tests` (needs approval; only if `test_commands` is set). Newer builds may add more; every side-effecting tool goes through the same approval event. Limits: at most 32 tool calls per model turn (extra calls are dropped and reported as a `tool_calls_dropped` event), 64 KB of assistant text, 256 KB of arguments per call.

Event types (ignore unknown types):

| type | data | UI |
| --- | --- | --- |
| `assistant` | `{text}` | show as the assistant message |
| `tool_result` | `{tool, result}` or `{tool, status: "NOT_APPROVED"}` | optional detail; `result.status` is `APPLIED`, `CONFLICT`, `ERROR`, `PASS`, `FAIL`, `TIMEOUT`, `UNKNOWN` where applicable |
| `approval_required` | `{action_id, fingerprint, tool, summary}`; `summary` is an object: `{kind:"apply", path, diff}` or `{kind:"run_tests", name, argv}` | show diff / command with Approve and Reject |
| `approval_expired` | `{action_id}` | the approval timed out (600 s); the action was not run |
| `state` | `{state, detail?}` with state `COMPLETED` / `FAILED` / `CANCELLED` | final status; `detail` on FAILED is only an exception class name or a reason such as "step limit reached" (messages are hidden on purpose) |

Approve/reject: `POST .../decision` `{action_id, fingerprint, approve}`, copying `action_id` and `fingerprint` from the event unchanged -> `{"accepted": true}`; **409** if there is no matching pending action (wrong fingerprint, already decided, task finished). Cancel: `POST .../cancel` -> `{state}`; a running test command is killed within about a second.

Honest labels: a proposal is not an edit. A file changed only after a `tool_result` for `apply_proposal` with `result.status == "APPLIED"`. A rejected or timed-out approval appears as `tool_result` with `status: "NOT_APPROVED"` (no `result`). Tests passed only when a `run_tests` result says `PASS`.

## 5. Check your wiring (all must hold)

1. `GET /api/code/availability` -> `available: true` when logged in; 401 otherwise (same for `tasks`, `events`, `decision`, `cancel`).
2. A second user cannot read the first user's task (404).
3. Starting twice with the same `idempotency_key` (same user, same body) returns the same `task_id`.
4. An instruction that makes the model edit a file produces `approval_required` and the file is unchanged until you approve; after approving, `apply_proposal` reports `APPLIED`.
5. Rejecting leaves the file unchanged.
6. Make `complete` raise: the task ends `FAILED`, and the rest of the chat still works.

The attachment's own tests live in the source repository (`tests/test_code_attach*.py`; run from the repository root); they are not part of the copied folders.

## 6. Known limits (do not paper over)

Tasks are in memory (a restart drops live tasks; activity logs persist only if enabled); at most `max_tasks` (64) run at once and 256 finished tasks are kept. A `resolve_workspace` path that does not exist yields 404. Files that are not UTF-8 (for example Shift-JIS) cannot be read or edited yet. See `docs/AUDIT_2026-10-24.md`. No delete/rename/Git. Text search only. Approved test commands run project code with the backend's privileges. Real-model behaviour is not measured yet (`kpi/REPORT.md` in the source repository). Privacy: absolute paths, the home directory and the OS user name never leave the process (they are replaced by `<workspace>`, `~`, `<user>` in tool output, errors and test output; the activity log stores only opaque hashes of workspace and user id). Your own `authorize` user id and `test_commands` argv are yours: do not put personal data in them.
