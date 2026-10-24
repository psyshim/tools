# host_client — the only thing a host installs

A powerless client for the coding service. Standard library plus the `protocol` package; **no** agent, tool, file, Git, subprocess, web-framework or model dependency (asserted by `tests/test_host_client.py`, which installs the kit into an empty directory and runs it with site-packages disabled).

## Install and remove

```bash
python -m hostkit_build.build_host_kit --out dist/host_kit      # reproducible wheel + SHA256SUMS + KIT_MANIFEST.json (source revision, per-file hashes)
pip install dist/host_kit/code_host_kit-1.0.0-py3-none-any.whl
pip uninstall code-host-kit                                      # removal leaves ordinary chat, login and uploads untouched
```

## What the host writes

Three callbacks it already has, plus one object:

```python
from host_client import AgentGateway
gateway = AgentGateway("https://coding-service.internal", service_credential)     # credential belongs to the HOST, not the end user

gateway.availability()                                                            # Available(modes, mutation) | Unavailable(reason); never raises
handle = gateway.start_coding(user_id, workspace_id, text, conversation_id=..., message_id=...)    # same (conversation, message) = same task
result = gateway.poll_coding(user_id, handle, resume_token)                       # PollResult(events, terminal, next_token) for your own UI loop
for event in gateway.observe_coding(user_id, handle, resume_token): ...           # or stream until terminal
gateway.cancel_coding(user_id, handle)                                            # the service's persisted state; never invented
gateway.submit_decision(user_id, handle, action_id, fingerprint, approve)         # optional; only with a reliable approval UI
```

`user_id` must be the identity the host verified; `workspace_id` must already have passed the host's own ACL check. Render `SafeDisplayEvent.html_text` (cleaned, bounded and escaped), never raw server fields. A complete working example is `examples/sample_host/` (about 60 lines of adapter).

## Behaviour you can rely on

- A proposal (`proposal_completed`) never means a file changed. Only a `mutation_applied` receipt does, and a default installation never produces one.
- Lost responses, restarts and double clicks do not create second tasks (stable idempotency key); duplicate events are rendered once; missing events show a visible gap marker.
- Another protocol major version, a rejected credential, a malformed answer or a down service all become `Unavailable` / `AgentUnavailable` with a short reason. Catch it, say "coding assistant unavailable", carry on.
- Nothing is retried blindly if it has side effects: an unclear decision outcome is resolved from the event log.
