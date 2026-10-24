# Integration review — sonnet-attach-boundaries versus agreed architecture

**Date:** 2026-10-24. **Basis:** GitHub comparison `main...sonnet-attach-boundaries` and source inspection of `code_attach/router.py`, `tools.py`, `session.py`, `coding_agent/git_ops.py`, `docs/HOST_HANDOFF.md`. **No local execution/test PASS claimed.**

## Verdict: BLOCK direct production merge / handoff; preserve feature work

This branch adds a substantial experimental code-attachment system. The change is valuable as a functional prototype, but must not silently replace the accepted detached agent architecture. Treat it as a device-side prototype and test fixture until physically repackaged.

### B1 — Blocking host/device authority inversion
`docs/HOST_HANDOFF.md` asks the receiving host to **copy both** `code_attach/` and `coding_agent/` into its process. `code_attach/router.py` uses FastAPI and constructs `Engine` inside `make_router`; `code_attach/tools.py` directly imports `Workflow`, `Workspace`, `FileOperations`, and `GitOperations`. Consequently the host process gains direct filesystem, test subprocess and Git capabilities. This contradicts ADR-001/003/004 (separate host-client distribution without local executor). **Required design correction:** move tool execution and workspace handles into device-agent distribution; make host install only network/DTO SDK; coding service owns task loop separately. Prohibit importing `coding_agent` in host package as a physical packaging constraint, not just lint.

### B2 — Unattended live workspace mutation conflicts with accepted policy
`ToolBox.t_apply_proposal` uses `Workflow.replace_text`, `FileOperations.apply` or `GitOperations.apply` on the live local workspace. User approval gates an action but **does not make SHA-check-then-replace an atomic compare-and-swap against an uncoordinated VS Code save**. ADR-001/003 require absent edit capability where that guarantee is unproven. **Required:** advertise these as explicit local experimental actions, not production-safe unattended write/commit. Default read/propose operates on pinned snapshot/revision and returns a patch artifact. Verify platform-specific apply separately.

### B3 — Task/events durability mismatch
`docs/HOST_HANDOFF.md` explicitly states tasks live in memory and restart drops them. The accepted contract requires persisted task identity, ordered resumable events and idempotency across response loss/restart. **Required:** Coding Service independently persists task/event state and performs replay; host must not implement recovery as a workaround.

### B4 — Host instructions conflict with the binding receiving-team scope
`docs/HOST_HANDOFF.md` asks the host to implement a model completion callback (provider translation), configure `test_commands`, and mount the tool-bearing router. ADR-004 gives the host only existing session, workspace ACL, attachment grant and chat-render callbacks. **Required:** mark Sonnet host handoff instructions prototype-only; do not transfer them to host team. Model providers and executable test profiles must be agent/device-side responsibilities.

### B5 — CI scope and evidence
`main` CI now discovers all `test_*.py` on Python 3.11/3.12; this does not test unpublished Sonnet branch artifacts in the main workflow. Tests must be actually run against the exact Sonnet commit/merge candidate, including contract, security and packaging isolation checks. Merely adding a comprehensive test suite does not prove isolated-process deployment.

## Non-overlapping integration path

1. Keep Sonnet's tested search, proposal/diff, encoding and Git planning code as **device-only candidate libraries**, not host imports.
2. Extract transport-neutral request/response schemas and make mock/service contract fixtures from them.
3. Replace the copying-based host handoff with a separately installable host-client, no local tool transitive dependencies.
4. Run an independent Coding Service (durable task/event owner) and Device (one local executor) in different processes.
5. Enable read/propose first; any mutable capability gets its own actual adversarial proof.
6. Merge candidate only after package isolation and read/propose vertical E2E; report real test evidence, not doc assertions.

## Work division

Haiku's merged work covers owner-lock location, inventory and boundary AST cleanup. Sonnet owns new feature implementation in `sonnet-attach-boundaries`. This review owns the architecture-integration assessment only. Avoid editing Sonnet's implementation in parallel until it has been reviewed/rebased.

**Status: architecture review COMPLETE, feature release still BLOCKED.**
