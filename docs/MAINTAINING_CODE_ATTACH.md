# Maintaining code_attach

## Module map (one reason to change each)

| Module | Layer | Owns | Must not |
| --- | --- | --- | --- |
| `router.py` | host-facing | HTTP translation, error -> status mapping | execute anything, import tools/session, touch files |
| `providers.py` | host-facing | neutral message <-> OpenAI/Anthropic shapes | call a model |
| `activity.py` | agent (re-export) | `ActivityLog`, `default_log_dir`, `display_path` for the host | add logic |
| `wire.py` | host-facing | pure mapping of internal events to protocol envelopes (stable ids) | import tools/session or do I/O |
| `protocol_router.py` | host-facing | the contract-shaped HTTP surface over `Engine` (envelopes, stable error codes, cursor, version check) | execute anything; return anything that does not validate against `protocol` |
| `engine.py` | agent | task registry, idempotency, ownership, capacity/eviction | know HTTP or auth |
| `session.py` | agent | the model/tool loop, approval state machine, event list, cancel | do I/O besides the sink |
| `schemas.py` | agent | tool schemas, `EFFECT_TOOLS` | hold behavior |
| `tools.py` | agent | `ToolBox`: the `t_*` tool implementations, proposals, approval summaries | spawn processes, define schemas |
| `testrun.py` | agent | the only place that starts processes: filtered env, cancel, tree-kill, bounded output | decide what to run (the host's argv list does) |
| `tools_edit.py` | agent | `EditTools` mixin: line-range edits, atomic batches, notebook cells, applied-change history and undo; all built on whole-file compare-and-swap (`Workflow.replace_bytes`) | write without a stored sha256 of what the proposal was based on |
| `tools_nav.py` | agent | `NavTools` mixin: `outline_file`, `find_symbol`, `project_overview` (read-only) | execute or write anything |
| `matching.py` | agent | locating edit targets (line-ending tolerant) and error messages that teach the model | guess across whitespace differences |
| `outline.py` | agent | pure structure extraction (Python via `ast`, heuristics elsewhere) | do I/O |
| `detect.py` | agent | project recon and *suggested* commands (read-only) | run anything; a suggestion is never runnable by the model |
| `testparse.py` | agent | pure parsing of runner/compiler output into failures and diagnostics | decide PASS/FAIL (the exit code does) |
| `privacy.py` | agent | scrubbing paths/home/user name from anything that leaves the process | |
| `globs.py` | agent | glob semantics | |

Primitives live in `coding_agent/` (`Workspace` path policy and walking, `Workflow` atomic edits, `text_encoding`, `file_ops`, `git_ops`, `activity_log`). `coding_agent` never imports `code_attach`. The layering is enforced by `tests/test_code_attach_boundaries.py` (AST import scan: every module must be classified there, so a new module fails the test until you decide its layer).

## Invariants (each has a test; do not weaken)

0. **Default posture is read/propose.** `make_router` defaults to `allow_mutation=False`; in that mode apply, run_tests and undo are not offered to the model, are refused if called, and no approval card can exist (`tests/test_code_attach_posture.py`). Enabling mutation is an explicit local-experimental choice (playbook phase 6 gates any real use).

1. Nothing is written, deleted, moved or committed before an approval event whose `fingerprint` the caller echoes back. Proposals are stored with their fingerprint; apply uses the stored proposal, never model-supplied content.
2. Stale files give `CONFLICT`; a transient OS error keeps the proposal retryable.
3. Test commands are fixed argv lists chosen by the host; the model picks only a name. They run with an allowlisted environment and are killed as a process tree on timeout/cancel.
4. Absolute paths, home directory and user name never appear in events, model context, approval cards or logs (`privacy.Scrubber`; `display_path` for anything shown to a person).
5. A model, log or tool failure ends a task as `FAILED`, never `COMPLETED`; a broken log never blocks cancel.
6. Sink I/O never happens under the lock pollers use; ordering is write-ahead (sink first, then visible).

## Adding a tool (checklist)

1. Schema in `schemas.py` (description says "Requires user approval" only for entries of `EFFECT_TOOLS`).
2. `t_<name>(...)` on `ToolBox`; parameters without defaults must be `required` in the schema.
3. A read-only or proposing tool must not write; if it needs to change disk it creates a proposal and the change happens in `t_apply_proposal` (add the `kind` there and an approval summary).
4. Anything returned to the model passes through `self.scrub` if it can contain paths or process output.
5. Add a test in `tests/test_code_attach_project.py` (happy path on the virtual project) and `tests/test_code_attach_abnormal.py` (refusals, conflicts, escapes).
6. `tests/test_code_attach_registry.py` fails if schema and method disagree.

## Running the tests

```bash
python -m unittest discover -s tests -p "test_code_attach*.py"     # about 1 minute: the attachment itself
python -m unittest discover -s tests                                # about 3 minutes: everything, including stress/large-script suites
python -m kpi.kpi --write                                           # progress KPI recomputed from evidence
python -m pyflakes code_attach                                      # also enforced by test_code_attach_registry
```

Known environment traps (Windows): creating symlinks needs privilege (those tests error in the baseline); do not pass backslash escapes or NUL bytes through shell heredocs when generating source (use a file-writing tool); files in the repo may be CRLF, so patch scripts must detect the line ending; subprocess output must be drained after killing the tree, never `subprocess.run(timeout=)`.

## Contract and conformance

`protocol/` (stdlib only) defines the public wire contract; `testkit/` holds the shared fixtures, an executable mock and two transports. `tests/test_protocol_conformance.py` runs identical scenarios against the mock and the real service; a scenario a world cannot prove must be listed in its `unsupported` table with a reason (it shows as UNSUPPORTED in `python -m examples.conformance_evidence`, never as a silent skip). Change the contract only by editing `protocol/` and its fixtures first. See `docs/MIGRATION_PHASES_0_2_REPORT.md`.

## Adversarial suite

`tests/test_code_attach_adversarial.py` is the regression net for hostile input: path matrix (read, propose AND apply), seeded argument fuzz over every schema, hostile model scripts, races, hostile HTTP bodies, hostile test output. When you add a tool it is covered automatically by the fuzz and the path matrix as long as its schema is registered. When a new bug is found, add the attack as a case here first, then fix it.

## Where things are decided

- Why this shape and what is deferred: `docs/ATTACH_V0.md`, `docs/ADR-004_THIN_HOST_SDK_BOUNDARY.md`.
- What the host writes: `docs/HOST_HANDOFF.md`.
- Findings and their status: `docs/AUDIT_2026-10-24.md`.
- Progress and denominators: `kpi/REPORT.md` (generated).
