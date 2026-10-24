"""Local execution composition root: workspace ownership is mandatory.

Use this facade for local edits. Constructing independent Workflow or
JournaledEdit objects does not grant multi-process ownership.
"""
from pathlib import Path

from .execution_owner import SingleExecutor
from .journal import OperationJournal
from .journaled_edit import JournaledEdit
from .workflow import Workflow

class WorkspaceEditRuntime:
    def __init__(self, workspace, journal_path):
        self.root = Path(workspace).resolve(strict=True)
        # Ownership before opening the journal or constructing edit tools.
        self.owner = SingleExecutor(self.root)
        try:
            database = Path(journal_path).resolve(strict=False)
            if database.is_relative_to(self.root):
                raise ValueError("journal database must be outside editable workspace")
            self.journal = OperationJournal(database)
            try:
                self.edit = JournaledEdit(Workflow(self.root), self.journal)
            except BaseException:
                self.journal.close()
                raise
        except BaseException:
            self.owner.close()
            raise
        self.closed = False

    def replace_text(self, operation_id, relative, expected_sha256, original, replacement):
        if self.closed:
            raise RuntimeError("runtime closed")
        return self.edit.replace_text(
            operation_id, relative, expected_sha256, original, replacement)

    def inspect(self, relative):
        if self.closed:
            raise RuntimeError("runtime closed")
        return self.edit.workflow.inspect(relative)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.journal.close()
            finally:
                self.owner.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
