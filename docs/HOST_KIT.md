# Host kit — one page for the host maintainer

This is the page to hand to the team that owns the chat backend. It replaces `HOST_HANDOFF.md` (prototype, in-process).

## What you install and what you write

You install one pure-Python wheel, `code-host-kit` (`host_client` + `protocol`, standard library only), and you write one thin adapter (about 60 lines: `examples/sample_host/adapter.py`). You do **not** install or import any agent, tool, file, Git, subprocess, model or web-framework code. The wheel is reproducible and carries a manifest with the source revision and per-file hashes.

```bash
python -m hostkit_build.build_host_kit --out dist/host_kit
pip install dist/host_kit/code_host_kit-1.0.0-py3-none-any.whl
pip uninstall code-host-kit     # removal leaves ordinary chat, login and uploads untouched
```

The adapter supplies three callbacks the host already has:

| Callback | Meaning |
|---|---|
| `identity(request)` | the verified user id (raise to refuse) |
| `can_access_workspace(user, workspace_id)` | **your** ACL decision. The service grants workspaces to the host, not to its users. |
| `deliver(user, conversation_id, event)` | render an event in your chat UI. Use `SafeDisplayEvent.html_text`, never raw fields. |

and one object: `AgentGateway(service_url, host_credential)` (`availability`, `start_coding`, `poll_coding`/`observe_coding`, `cancel_coding`, `submit_decision`).

## Rules that keep the host powerless

1. The credential identifies the **host**; the end user is passed as `user_id` and is namespaced by the service as `<host_id>:<user>`.
2. A `proposal_completed` event never means a file changed. Only `mutation_applied` does, and a default installation never produces one.
3. If the service is down, absent, refused (bad credential) or speaks another protocol major, the gateway answers `Unavailable`. Show "coding assistant unavailable" and carry on; chat is unaffected.
4. Same `(conversation_id, message_id)` means the same task, so retries and double clicks never start a second one. Do not add retries of your own.
5. No absolute path or personal information appears in events; if you see one, that is a service bug, report it.

## What you do **not** operate

The Coding Service (task and event store, model access) and the Device Agent (the only process that touches the project) are operated separately. See `docs/OPERATOR_GUIDE.md`. Model access comes from the service's configuration; the host never holds model keys.

## Proof you can rerun

`python -m examples.conformance_evidence --output evidence.json` runs the contract, the shared conformance suite against the mock and the real service (read/propose and experimental, over HTTP, in separate processes), the host client, the sample host, the device agent, restart/crash recovery and the write guard. Every scenario a given implementation cannot prove is listed as `UNSUPPORTED` with a reason, never silently skipped.

## Not for production

`code_attach.make_router` / `make_protocol_router` run the tools inside the host process. They refuse to mount unless called with `prototype=True`, and exist only for the local demo (`examples/local_app.py`) and the prototype tests.
