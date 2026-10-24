"""Declarative tool schemas shown to the model, and which tools need user approval.

Adding a tool: add its schema here, implement `t_<name>` in tools.py (tests/test_code_attach_registry.py enforces the pairing).
"""
EFFECT_TOOLS = {"apply_proposal", "run_tests"}  # side effects happen only after an approval event

SCHEMAS = [
    {"name": "list_dir", "description": "List files under a workspace-relative directory.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}}}},
    {"name": "find_files", "description": "Find files by glob (e.g. '*.ini', 'settings*', 'src/**/*.py') anywhere under a directory, skipping build/vendor/VCS folders.",
     "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "search_text", "description": "Case-insensitive substring search across text files up to 8 MiB (no regex). Set whole_word to ignore partial-word hits, or terms for alternative strings (any matches). Returns at most five hits per file with line and column; start_line skips earlier lines in EVERY searched file, so to page through one noisy file set path to that file and start_line to the line after its last hit. For 'where is X defined or used' prefer find_symbol.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "start_line": {"type": "integer"},
                                                      "whole_word": {"type": "boolean"}, "terms": {"type": "array", "items": {"type": "string"}}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read a text file up to 8 MiB (max 200 lines per call). Set start_line to read a later page; end_line is optional. Lines over 2000 characters are shortened and listed in truncated_lines; read such a line with start_line=<n>, column and width.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "column": {"type": "integer"}, "width": {"type": "integer"}}, "required": ["path"]}},
    {"name": "propose_edit", "description": "Propose replacing one exact, unique text occurrence in a file (line endings are matched flexibly). If it fails, the error says where the text really is, whether whitespace differs, or where the duplicates are. Nothing is written until apply_proposal is approved.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "propose_new_file", "description": "Propose creating a new file. Nothing is written until apply_proposal is approved.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "apply_proposal", "description": "Apply a proposal by id. Requires user approval.",
     "parameters": {"type": "object", "properties": {"proposal_id": {"type": "string"}}, "required": ["proposal_id"]}},
    {"name": "run_tests", "description": "Run a pre-configured test command by name. Requires user approval.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

SCHEMAS.extend([
    {"name": "git_inspect", "description": "Inspect local Git status, diff, staged_diff, log or branches. No arbitrary commands or network operations.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "diff", "staged_diff", "log", "branches"]}, "path": {"type": "string"}}, "required": ["action"]}},
    {"name": "propose_git_commit", "description": "Propose a local commit of explicitly selected files in an existing branch. Refuses pre-existing staged changes. Apply requires approval. Built-in Git EOL/encoding conversion is pinned; external filters, hooks and signing are disabled. No push/merge/rebase/reset/clean.",
     "parameters": {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}}, "required": ["paths", "message"]}},
    {"name": "propose_delete_file", "description": "Propose deleting a regular file after saving a persistent local recovery backup. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "propose_move_file", "description": "Propose moving or renaming one file inside the workspace. Never overwrites a destination. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}},
    {"name": "propose_copy_file", "description": "Propose copying one file without replacing existing files. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}},
    {"name": "propose_directory", "description": "Propose creating a directory including parents, or removing an empty directory. No recursive deletion. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "operation": {"type": "string", "enum": ["create", "remove_empty"]}}, "required": ["path", "operation"]}},
    {"name": "list_file_backups", "description": "List up to 100 persistent file recovery backups for this workspace, without contents.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_restore_file", "description": "Propose restoring a recovery backup to its original path or an optional different workspace path. Never overwrites. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"backup_id": {"type": "string"}, "path": {"type": "string"}}, "required": ["backup_id"]}},
])

SCHEMAS.extend([
    {"name": "outline_file", "description": "List the definitions in one file (classes, functions, methods, types, headings, sections) with line numbers. Python is parsed exactly; other languages use heuristics. Use it to see a file's shape before reading it.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "find_symbol", "description": "Find where a function, class, method or type is defined and where its name is used, across the project. name may be qualified (Class.method). Optional kind filters definitions (function, class, method, type, interface, enum, constant, heading).",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}, "kind": {"type": "string"}}, "required": ["name"]}},
    {"name": "project_overview", "description": "Describe the project: languages, ecosystems (Python, Node, .NET, Java, Go, Rust, ...), test folders, configuration files, and the commands the host has enabled. Suggested commands in the result are information only; you can run only the host-enabled ones with run_tests.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "propose_replace_lines", "description": "Propose replacing an inclusive 1-based line range. expect must be the current text of those lines (copy it from read_file); a mismatch is refused and the real lines are shown. Use when the text is not unique enough for propose_edit. Nothing is written until apply_proposal is approved.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "expect": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "start_line", "end_line", "expect", "new_text"]}},
    {"name": "propose_edits", "description": "Propose several edits across one or more files as ONE all-or-nothing change (one approval, rolled back entirely if any step fails). Each item is {path, old, new} to edit or {path, content} to create a file. Edits to the same file apply in order. Use for renames and refactors that must stay consistent.",
     "parameters": {"type": "object", "properties": {"edits": {"type": "array", "items": {"type": "object"}}}, "required": ["edits"]}},
    {"name": "propose_undo", "description": "Propose reversing a change that was applied in this task, identified by the applied_id in its APPLIED result. Refused if the files changed again since. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"applied_id": {"type": "string"}}, "required": ["applied_id"]}},
    {"name": "read_notebook", "description": "Read a Jupyter notebook as numbered cells (type, source, output count) instead of raw JSON.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_cell": {"type": "integer"}, "count": {"type": "integer"}}, "required": ["path"]}},
    {"name": "propose_notebook_cell", "description": "Propose replacing, inserting after (index -1 inserts first) or deleting one notebook cell by index. Replacing a code cell clears its stale outputs. Apply requires approval.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "index": {"type": "integer"}, "operation": {"type": "string", "enum": ["replace", "insert_after", "delete"]}, "source": {"type": "string"}, "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]}}, "required": ["path", "index"]}},
])

for _schema in SCHEMAS:
    if _schema["name"] in {"read_file", "search_text", "propose_edit", "propose_new_file"}:
        _schema["parameters"]["properties"]["encoding"] = {
            "type": "string", "enum": ["utf-8", "utf-8-sig", "cp932", "shift_jis", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]}
        _schema["description"] += " BOM encodings are detected automatically. Specify encoding for known legacy text (e.g. cp932); decoding is strict and original encoding/BOM are preserved."
