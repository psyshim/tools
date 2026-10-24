# tools — detachable coding agent

A backend-independent local coding-agent foundation. Initial implementation is read-only and Python standard-library only.

## Scope

- Workspace-scoped directory discovery, bounded partial file reads and SHA-256 file hashes
- Loopback HTTP adapter with bearer authentication
- The HTTP service exposes no file mutation, shell execution, Git commits, relay or AI model integration. `workflow.py` and `patching.py` contain write helpers that are not reachable from the service and remain private prototypes

## Local run

Requires Python 3.11+.

```bash
export CODING_AGENT_TOKEN='replace-with-a-random-secret'
python -m coding_agent.service --workspace /path/to/workspace --port 8765
python -m unittest discover -s tests -v
```

PowerShell: `$env:CODING_AGENT_TOKEN = 'replace-with-a-random-secret'`.

`POST /v1/workspace/list` accepts `{ "path": ".", "depth": 1 }`.

`POST /v1/workspace/read` accepts `{ "path": "README.md", "start_line": 1, "end_line": 100 }`.

Supply `Authorization: Bearer <token>` on every request.

This service is development-only. Do not expose it to the internet. A future relay must enforce workspace authorization, device authentication, audit logs, idempotent operations, secret redaction and explicit approvals for side effects.

Planned removable layers: agent-core (planning and execution journal), local-agent (workspace tools), adapters (versioned host connection). The host backend must never be imported by this module.
