# Upstream correction checklist (Sonnet branch)

**Integration verdict: BLOCK for production host handoff; retain functional prototypes.**

Full detailed remediation and acceptance criteria:
- [Upstream migration playbook on main](https://github.com/psyshim/tools/blob/main/docs/UPSTREAM_MIGRATION_PLAYBOOK.md)
- [Independent audit on main](https://github.com/psyshim/tools/blob/main/docs/REVIEW_SONNET_ATTACH_INTEGRATION_2026-10-24.md)
- [Single-authority decision](https://github.com/psyshim/tools/blob/main/docs/ADR-001_SINGLE_AUTHORITY.md)
- [Host-client-only handoff decision](https://github.com/psyshim/tools/blob/main/docs/ADR-004_THIN_HOST_SDK_BOUNDARY.md)

## Please correct in small, independently testable PRs

- [ ] P0: Mark `docs/HOST_HANDOFF.md` and FastAPI `make_router` path **prototype/local-only**, not the receiving host's production instructions. Disable live mutation capabilities by default.
- [ ] P1: Preserve observable behavior in reproducible fixtures (read/search, proposals, encoding, approval, bounded tests, Git planning); record actual PASS/BLOCK.
- [ ] P2: Extract a pure versioned protocol DTO/schema and independent mock; test the same contract against actual service.
- [ ] P3: Deliver separately installable **host-client-only** distribution. Host must not import or copy `code_attach`, `coding_agent` or executable tools.
- [ ] P4: Move tool adapters and workspace authority to a **separate device process**, with one execution owner and private journal. Keep current feature code as candidate device libraries.
- [ ] P5: Persist task identity, events, dedupe and state in Coding Service; prove restart/replay/cross-actor denial.
- [ ] P6: Enable read/propose first. Do not advertise unattended live VS Code writes, test execution or Git commits until independent safety gates pass.
- [ ] P7: Remove/quarantine obsolete entrypoints and issue a pinned, tested host kit.

### Acceptance rule for each phase

Provide: exact old import/route removed; new owner; fixture(s); negative regression; test command/output; SHA; rollback. Never claim completed deployment from unit tests alone.

**Do not merge a large rewrite solely to satisfy this checklist.** Preserve useful feature code and move one physical boundary per PR. Other workers may be editing nearby files; verify current HEAD and avoid overwriting unrelated changes.
