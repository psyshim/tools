# Implementation inventory — doc/code contradictions

**As of:** main `0fa9255` (2026-10-24). **Purpose:** the tracked implementation inventory that ADR-002 §1 asks for. Each row records a claim, what the code actually does, and the evidence. This file makes no claim of test PASS. Gate status lives in ADR-002 §6.

## Contradictions found

| # | Source of claim | Claim | What the code does | Evidence | Disposition |
| --- | --- | --- | --- | --- | --- |
| C1 | `README.md` (Scope) | "No file mutation … yet" | Two write paths exist: `workflow.py` and `patching.py` both write files with `os.replace`. Neither is reachable from the HTTP service, which only routes `/v1/workspace/list` and `/v1/workspace/read`. | `README.md:9`; `coding_agent/workflow.py:60-68`; `coding_agent/patching.py:32-39`; `coding_agent/service.py` route check | Reword README to say "no mutation exposed via the service"; ADR-002 already marks these as private helpers to demote. |
| C2 | `docs/INTEGRATION_CONTRACT_V0_1.md` (status matrix, row "Mock relay of read calls") | Mock lives at `coding_agent/mock_backend.py` | File does not exist. The mock is `host_mock/server.py`. | `ls coding_agent/mock_backend.py` → not found; ADR-002 §1 table row 7 | Update the matrix path. |
| C3 | `docs/INTEGRATION_CONTRACT_V0_1.md` (status matrix, row "durable journal") | "Task orchestration / event cursor / durable journal: planned" | `coding_agent/journal.py` implements a sqlite-backed journal (`sqlite3.connect`). It has no schema versioning, actor, approval, or recovery classifier, as ADR-002 notes. | `coding_agent/journal.py:4,14`; ADR-002 §1 row `journal.py` | Mark as "prototype exists, not conformant" rather than "planned". |
| C4 | `coding_agent/execution_owner.py` vs ADR-001 | Owner lease must live outside the worktree | **Resolved in `fix: keep owner lock outside the enrolled workspace`.** Lock now lives in a private state dir keyed by root hash; a test asserts the workspace stays empty. | `tests/test_execution_owner.py` | Device-private registry (ADR-002 §2) still pending. |
| C5 | `tests/test_architecture_boundaries.py` vs ADR-002 | Boundary tests prove isolation | Tests are an AST import scan. They do not prove runtime privilege isolation. The agent check was also falsely failing on same-package relative imports (fixed in `0fa9255`). | `tests/test_architecture_boundaries.py`; ADR-002 §1 last row | Docstring now states it is an import scan only. Not counted as isolation proof. the gates in ADR-002 §6 need sandboxed process tests. |

## Not contradictions (checked and consistent)

- `service.py` exposes only read routes and binds to loopback with bearer auth (`hmac.compare_digest`). This matches ADR-002's "device-local diagnostic port only" disposition.
- `host_mock/` does not import `coding_agent` in its own source (HOST_FORBIDDEN check passes).

## Open items for the next change set

1. Apply the C1–C3 doc corrections. These are doc-only and safe to merge.
2. C4 and C5 need code and tests. They are not doc-only work and should be separate branches.
