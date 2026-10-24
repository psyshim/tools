# Lightweight mock backend — working read-only vertical slice

The mock is a standalone stdlib-only HTTP server. It simulates **only** the minimal host-to-agent connection, not a full chat backend, user authentication, approval UI, or the planned task API. Both servers bind to loopback and must not be exposed publicly.

Run from repository root using Python 3.11+ in two terminals:

Terminal A:

```sh
export CODING_AGENT_TOKEN='agent-secret-change-me'
python -m coding_agent.service --workspace . --port 8765
```

Terminal B:

```sh
export CODING_AGENT_TOKEN='agent-secret-change-me'
export MOCK_HOST_TOKEN='host-secret-change-me'
python -m host_mock.server --agent-url http://127.0.0.1:8765 --port 8766
```

On Windows PowerShell use `$env:CODING_AGENT_TOKEN='...'` and `$env:MOCK_HOST_TOKEN='...'`.

Try:

```sh
curl -X POST http://127.0.0.1:8766/mock/list -H 'Authorization: Bearer host-secret-change-me' -H 'Content-Type: application/json' -d '{"path":".","depth":1}'
```

`POST /mock/list` and `POST /mock/read` delegate to existing Local Agent read endpoints. They have separate host and agent bearer tokens. The host mock forwards only allowlisted routes; failures map to 401, 404, 502 or 503 and are not falsely reported as successful. The mock intentionally has no shell, write, Git, model or attachment operations.

Test: `python -m unittest discover -s tests -p 'test_mock_backend.py' -v`.

The **production** host wrapper must satisfy [HOST_BACKEND_WRAPPER_REQUIREMENTS.md](HOST_BACKEND_WRAPPER_REQUIREMENTS.md), including per-user workspace authorization, durable idempotency, attachments, events, approval, and version negotiation; this mock is **not** a reference implementation of those production controls.
