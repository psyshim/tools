# Host handoff — one page

Goal: the chat backend (FastAPI) gains "modify and test a local project" by writing **only the adapter below**. Everything else (tools, approval, test running, task loop) ships in `code_attach/`.

**You do not**: call any tool yourself, run commands, touch project files, write Git/test logic, add a task state machine, or add retries. If you feel the need to, stop: it belongs to `code_attach`.

## 1. Install

Copy the `code_attach/` and `coding_agent/` folders next to your backend code. Python 3.11+, `fastapi` and `pydantic` (already in your backend). No other dependency.

## 2. Write three functions (the adapter)

```python
from code_attach import make_router
from code_attach.providers import to_openai, from_openai      # or to_anthropic / from_anthropic

def complete(messages, tools):
    """Use the model call your backend ALREADY has. Convert in, call, convert out. No keys are handled by code_attach."""
    payload = to_openai(messages, tools)          # -> {"messages": [...], "tools": [...]}
    reply = your_existing_chat_call(**payload)    # returns the assistant message dict
    return from_openai(reply)                     # -> {"text": str, "tool_calls": [...]}

def authorize(request):
    """Your existing session auth. Return a stable user id; raise HTTPException(401) otherwise."""

def resolve_workspace(principal, workspace_id):
    """Your ACL. Return the local folder path this user may use for workspace_id, else None."""
```

## 3. Mount it

```python
app.include_router(
    make_router(complete, authorize, resolve_workspace,
                test_commands={"unit": ["python", "-m", "pytest", "-x", "-q"]},   # fixed argv; omit to disable testing
                event_sink=ActivityLog(log_dir).record),                           # optional; see docs/ATTACH_V0.md (logging)
    prefix="/api/code")
```

The backend must run on the machine that holds the projects. Do not expose the prefix to unauthenticated users.

## 4. Frontend contract (4 calls)

| Do | Call |
| --- | --- |
| start | `POST /api/code/tasks` `{workspace_id, instruction, idempotency_key}` -> `{task_id}` |
| watch | `GET /api/code/tasks/{id}/events?after=N` every ~1 s; render `assistant`; stop when `terminal` is true |
| approve | on event `approval_required`: show `data.summary` (diff or test command) with Approve/Reject; `POST .../decision` `{action_id, fingerprint, approve}` copying `action_id` and `fingerprint` unchanged |
| cancel | `POST .../cancel` |

Label results honestly: a proposal is not an edit. A change exists only after an `apply_proposal` tool_result with `status: APPLIED`; tests passed only when a `run_tests` result says `PASS`.

## 5. Check your wiring (all must hold)

1. `GET /api/code/availability` returns `available: true` for a logged-in user and 401 otherwise.
2. A second user cannot read the first user's task (404).
3. Starting the same `idempotency_key` twice returns the same `task_id`.
4. A task that asks to change a file produces an `approval_required` event, and the file is unchanged until you approve.
5. Rejecting leaves the file unchanged.
6. Stop the model call (make `complete` raise): the task ends `FAILED`, chat elsewhere still works.

Run the package's own tests before and after copying: `python -m unittest tests.test_code_attach tests.test_code_attach_abnormal tests.test_code_attach_project tests.test_code_attach_boundaries`.

## 6. Known limits (do not paper over)

In-memory tasks (restart loses live tasks; activity logs persist if enabled); no delete/rename/Git; text search only; approved test commands run project code with the backend's privileges. Real-model behavior is not yet measured; see `kpi/REPORT.md`.
