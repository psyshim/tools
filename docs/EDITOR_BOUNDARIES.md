# Experimental editor and VS Code boundary

The replacement helper in `coding_agent/patching.py` is a **locally tested primitive**, not enabled through HTTP or the mock backend.

It checks expected SHA-256, denies escaping paths and external symlinks, and atomically replaces a file through a sibling temporary file. Its process-local lock and recheck do **not** prevent external programs such as VS Code from writing during the final race window. Do not claim cross-process atomic compare-and-swap. Other pending concerns: Windows replacement semantics, hardlinks, metadata, backup/journal, approval and idempotency.

Before allowing unattended writes: choose and document an external-editor concurrency policy, audit filesystem aliases/hardlinks, persist journal and recovery data, bind approval to exact expected hash and patch, and test races. The mock backend remains read-only. No user's live VS Code workspace was modified.

Local test command: `python -m unittest discover -s tests -p 'test_patching.py' -v`.
