# code_attach v0 — host wiring (1-day delivery)

**Status:** IMPLEMENTED and TESTED locally (`tests/test_code_attach.py`, scripted fake model, no real LLM or real chat app yet). **INTEGRATED: no.**

## Deviation from ADR-004 (decided 2026-10-24, owner-approved)

To deliver edit + test in one day, v0 runs **in the host's FastAPI process on the same PC as the projects**. The service/device split, durable journal, signed claims and separate SDK package are deferred. Consequences:

- No crash recovery: tasks, proposals and events live in memory and are lost on restart.
- Task ownership is by host-supplied principal only; no device-side policy independent of the host.
- Apply and test run are protected by per-action user approval bound to a fingerprint (diff / argv), not by a separate device authority.
- Do not expose the router beyond the host's authenticated users.

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
- Symlink tests cannot run on Windows without privilege (pre-existing, 3 tests).
- Plain-text unique replace and new-file creation only; no delete/rename, no Git.
