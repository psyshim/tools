# Operator guide — Coding Service and Device Agent

Two processes, deliberately separate. The **Device Agent** runs on the machine that holds the projects and is the only process that reads or writes them. The **Coding Service** owns tasks, events, the model and the loop; it can reach a project only through the device's HTTP API and only for workspaces the device grants.

## 1. Device Agent

`grants.json` (absolute paths; the file is refused as a whole, with every problem listed, if anything is malformed or unsafe):

```json
{
  "device_id": "laptop-1",
  "token_sha256": "<sha256 hex of the bearer token the service will present>",
  "state_dir": "C:/Users/me/.device-agent",
  "workspaces": {
    "shop": {"root": "C:/Users/me/code/shop", "capabilities": ["read", "propose"]}
  }
}
```

```bash
python -m device_agent --config grants.json --port 8780
```

- Binds loopback only. A non-loopback client is refused unless `allow_remote_clients` is set deliberately.
- `state_dir` (journal, locks, recovery copies) must be outside every workspace. Workspace roots must not be nested.
- Effects are separate grants and need `"experimental_mutation": true`: `apply`, `git_commit`, `test` (with `test_commands`). Without them the capability does not exist; the model cannot ask its way into it.
- Exit codes: `2` bad grants, `3` another device agent already owns the same user's workspaces (one executor at a time).

## 2. Coding Service

`service.json`:

```json
{
  "db_path": "C:/svc/state.db",
  "allow_mutation": false,
  "model": {"openai_compatible": {"base_url": "https://models.internal/v1", "model": "small-coder", "api_key_env": "SVC_MODEL_KEY"}},
  "devices": {"laptop": {"url": "http://127.0.0.1:8780", "token_env": "SVC_LAPTOP_TOKEN"}},
  "hosts": {"chat-backend": {"credential_sha256": "<sha256 hex of the host credential>",
                               "workspaces": {"shop": {"device": "laptop", "workspace": "shop"}}}}
}
```

```bash
python -m coding_service --config service.json --port 8790
```

Secrets are never in the file: hosts are identified by the hash of their credential; device tokens and the model key come from the environment variables named. `model` may also be `"path/to/model.py:complete"`. `http` device URLs are accepted only on loopback. Exit codes: `2` bad config, `4` the task store cannot be opened or read (do not retry blindly: inspect it), `5` the service must not use this database: **another service already owns it** (retry later) or it sits somewhere a lease cannot protect (network location, hard-linked file: fix the path).

Mutation needs both sides: `allow_mutation: true` in the service **and** an effect grant on the device. Either alone leaves the installation read/propose.

## 3. Restart behaviour (what to expect)

- Tasks and events are durable (SQLite WAL, full sync). A restarted service resumes tasks that never started and closes tasks that were running as `FAILED` ("interrupted"); the stale approval card cannot be answered.
- If the process died while an **approved effect** was in flight, the task ends `NEEDS_RECONCILIATION`. It is never reported as success and never retried.

### 3.1 One service per task database

A service takes an OS-level lease (`<db>.service.lock`, next to the database) before it opens the database or loads the model, so a second service on the same database exits with code 5 before any recovery. Do not delete or replace that file while a service is alive. The lease is a local-filesystem guarantee, so a database on a Windows network location or with more than one name (hard link) is refused at start (exit code 5). Network filesystems on other platforms cannot be detected cheaply: keep the database on a local disk. Usage, export, backup and prune commands keep working while a service runs; restore writes to a NEW path.

## 4. Runbook: `NEEDS_RECONCILIATION`

1. Take the `op_id` from the task's `mutation_not_applied` notice (status `UNKNOWN_OUTCOME`).
2. Ask the device for the truth: `GET /v1/operations/<op_id>` with the device bearer token. State `SUCCEEDED` means the change happened and the recorded receipt is returned; `UNKNOWN` means the device died between the claim and the record.
3. For `UNKNOWN`, compare the file with the proposal diff (the approval card has it). The write is an atomic replace, so the change is either fully present or absent.
4. Fix the project by hand if needed and tell the user. The service never retries an `UNKNOWN` operation.

## 5. Known limits (stated, not hidden)

- On Windows a file held open for writing by another program, or opened without delete sharing, blocks the apply safely. A program that saves by renaming over the file between the final check and the swap is a documented residual risk. POSIX has no equivalent guard.
- An approval prompt alone does not make a live-worktree write safe against a concurrent human save; the stale-content check at decision time and the guard are what do.

## 6. Test-only switches

`CODING_SERVICE_ALLOW_FAILPOINTS` and `DEVICE_AGENT_ALLOW_FAILPOINTS` (with a `*_FAILPOINT` name) make a process kill itself at a chosen point, for the crash tests. Never set them in a real installation.

## 7. Logs and privacy

Events, logs and UI show workspace-relative paths only. Source excerpts and diffs may appear in the store; keep `db_path` and the device `state_dir` private.
