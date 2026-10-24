"""Edit tools beyond single exact-text replacement: line ranges, atomic multi-file batches, notebook cells, undo.

All of them follow the core rule: a `propose_*` call computes the complete new file content and stores it with the
sha256 of what it was based on; nothing touches disk until `apply_proposal` is approved, and then each file is replaced
only if it still has that sha256 (compare-and-swap). Applied changes are remembered (pre-image bytes) so they can be undone.
"""
import difflib
import hashlib
import json
import re
import uuid
from coding_agent.workflow import Conflict
from coding_agent.text_encoding import decode_source, encode_source, encode_new_source

from .matching import MatchError, adapt_eol, locate

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DIFF = 64000
MAX_BATCH_EDITS = 20
MAX_APPLIED = 50
MAX_APPLIED_BYTES = 64 * 1024 * 1024
MAX_CELL_PREVIEW = 2000

def _sha(data):
    return hashlib.sha256(data).hexdigest()

def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def _unified(path, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}"))

def _norm(text):
    return text.replace("\r\n", "\n").rstrip("\n")

class EditTools:
    """Mixin for ToolBox (uses ws, flow, files, proposals, applied, _lock)."""

    # --- helpers ---
    def _read_text(self, path, encoding=None):
        info = self.flow.inspect(path)
        data = self.ws.resolve(path).read_bytes()
        text, codec, prefix = decode_source(data, encoding)
        return data, info["sha256"], text, codec, prefix

    def _remember(self, items):
        """Keep pre-images of an applied change so propose_undo can reverse it. Returns the applied id."""
        aid = uuid.uuid4().hex[:8]
        with self._lock:
            self.applied[aid] = items
            size = lambda: sum(len(i["before"] or b"") for rec in self.applied.values() for i in rec)  # noqa: E731
            while len(self.applied) > MAX_APPLIED or (len(self.applied) > 1 and size() > MAX_APPLIED_BYTES):
                self.applied.popitem(last=False)
        return aid

    def _propose_write(self, kind, items, summary, diff, label):
        if len(diff) > MAX_DIFF:
            raise ValueError("proposal diff too large; split it into smaller changes")
        pid = uuid.uuid4().hex[:12]
        body = [{"op": i["op"], "path": i["path"], "sha": i.get("sha"), "new_sha": _sha(i["new_bytes"]) if i.get("new_bytes") is not None else None} for i in items]
        with self._lock:
            self.proposals[pid] = {"kind": kind, "path": items[0]["path"], "items": items, "diff": diff, "label": label,
                                   "summary": {**summary, "diff": diff}, "fingerprint": _digest(body)}
        return {"proposal_id": pid, "diff": diff, "summary": {k: v for k, v in summary.items() if k != "diff"}}

    # --- line-range replacement ---
    def t_propose_replace_lines(self, path, start_line, end_line, expect, new_text, encoding=None):
        if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line < start_line or end_line - start_line > 2000:
            raise ValueError("invalid line range (1-based, inclusive, at most 2000 lines)")
        if not isinstance(expect, str) or not isinstance(new_text, str) or len(new_text) > 100000:
            raise ValueError("expect and new_text must be strings (new_text at most 100000 characters)")
        data, sha, text, codec, prefix = self._read_text(path, encoding)
        lines = text.splitlines(True)
        if end_line > len(lines):
            raise MatchError(f"the file has {len(lines)} lines; end_line {end_line} is past the end")
        region = "".join(lines[start_line - 1:end_line])
        if _norm(region) != _norm(expect):
            raise MatchError(f"lines {start_line}-{end_line} are not what you expected. They actually contain:\n{region[:1500]}\nRe-read the file and retry with the exact current text in `expect`.")
        start = sum(len(l) for l in lines[:start_line - 1])
        end = start + len(region)
        replacement = adapt_eol(text, start, end, new_text)
        if replacement and region.endswith(("\n", "\r\n")) and not replacement.endswith("\n"):
            replacement += "\r\n" if "\r\n" in region else "\n"
        after = text[:start] + replacement + text[end:]
        new_bytes = encode_source(after, codec, prefix)
        if len(new_bytes) > MAX_FILE_BYTES:
            raise ValueError("updated file too large")
        diff = _unified(path, text, after)
        item = {"op": "write", "path": path, "sha": sha, "new_bytes": new_bytes}
        return self._propose_write("write", [item], {"kind": "edit", "path": path, "encoding": codec}, diff, f"replace lines {start_line}-{end_line}")

    # --- atomic multi-file batch ---
    def t_propose_edits(self, edits):
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_BATCH_EDITS:
            raise ValueError(f"edits must be a list of 1 to {MAX_BATCH_EDITS} items")
        state = {}   # path -> working copy; edits to one file apply in order, each on the result of the previous
        for n, item in enumerate(edits, 1):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError(f"edit {n}: each item needs a string path")
            path = item["path"]
            try:
                if "content" in item:
                    target = self.files.resolve(path)
                    if path in state or target.exists() or target.is_symlink():
                        raise ValueError("file already exists")
                    if not isinstance(item["content"], str):
                        raise ValueError("content must be a string")
                    codec = item.get("encoding", "utf-8")
                    new_bytes = encode_new_source(item["content"], codec)
                    state[path] = {"op": "create", "sha": None, "text": item["content"], "before_text": "", "codec": codec, "prefix": b"", "new_bytes": new_bytes, "created": True}
                    continue
                old, new = item.get("old"), item.get("new")
                if not isinstance(old, str) or not isinstance(new, str) or len(old) > 20000 or len(new) > 20000:
                    raise ValueError("old and new must be strings of at most 20000 characters")
                if path not in state:
                    data, sha, text, codec, prefix = self._read_text(path, item.get("encoding"))
                    state[path] = {"op": "write", "sha": sha, "text": text, "before_text": text, "codec": codec, "prefix": prefix}
                st = state[path]
                start, end = locate(st["text"], old)
                st["text"] = st["text"][:start] + adapt_eol(st["text"], start, end, new) + st["text"][end:]
            except ValueError as exc:
                raise type(exc)(f"edit {n} ({path}): {exc}") from None
        items, diff_parts, files = [], [], []
        for path, st in state.items():
            new_bytes = st.get("new_bytes") if st.get("created") else encode_source(st["text"], st["codec"], st["prefix"])
            if len(new_bytes) > MAX_FILE_BYTES:
                raise ValueError(f"{path}: updated file too large")
            items.append({"op": st["op"], "path": path, "sha": st["sha"], "new_bytes": new_bytes})
            diff_parts.append(_unified(path, st["before_text"], st["text"]))
            files.append({"path": path, "op": st["op"]})
        diff = "".join(diff_parts)
        return self._propose_write("write_batch", items, {"kind": "batch_edit", "files": files}, diff, f"{len(edits)} edits in {len(items)} files")

    # --- appliers (called from t_apply_proposal after approval) ---
    def _apply_write(self, p):
        item = p["items"][0]
        r = self.flow.replace_bytes(item["path"], item["sha"], item["new_bytes"])
        aid = self._remember([{"op": "write", "path": item["path"], "before": r["before"], "after_sha": r["sha256"]}])
        return {"status": "APPLIED", "path": item["path"], "sha256": r["sha256"], "applied_id": aid}

    def _undo_step(self, step):
        """Reverse one applied step if the file is still exactly as that step left it."""
        path = step["path"]
        if step["op"] == "create":
            target = self.files.resolve(path)
            if _sha(target.read_bytes()) != step["after_sha"]:
                raise Conflict("stale SHA-256")
            target.unlink()
        elif step["op"] == "remove":
            self.files._write_exclusive(self.files.resolve(path), step["before"])
        else:
            self.flow.replace_bytes(path, step["after_sha"], step["before"])

    def _apply_batch(self, p):
        items = p["items"]
        for item in items:   # phase 1: verify everything before touching anything
            if item["op"] == "create":
                if self.files.resolve(item["path"]).exists():
                    return {"status": "CONFLICT", "detail": f"{item['path']} already exists", "applied": []}
            elif _sha(self.ws.resolve(item["path"]).read_bytes()) != item["sha"]:
                return {"status": "CONFLICT", "detail": f"{item['path']} changed since the proposal", "applied": []}
        done = []
        try:
            for item in items:   # phase 2: apply in order, remembering how to reverse each step
                if item["op"] == "create":
                    target = self.files.resolve(item["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.files._write_exclusive(target, item["new_bytes"])
                    done.append({"op": "create", "path": item["path"], "before": None, "after_sha": _sha(item["new_bytes"])})
                elif item["op"] == "remove":
                    target = self.ws.resolve(item["path"])
                    before = target.read_bytes()
                    target.unlink()
                    done.append({"op": "remove", "path": item["path"], "before": before, "after_sha": None})
                else:
                    r = self.flow.replace_bytes(item["path"], item["sha"], item["new_bytes"])
                    done.append({"op": "write", "path": item["path"], "before": r["before"], "after_sha": r["sha256"]})
        except (Conflict, OSError, ValueError) as exc:
            failed = []
            for step in reversed(done):   # all or nothing
                try:
                    self._undo_step(step)
                except (Conflict, OSError, ValueError):
                    failed.append(step["path"])
            if failed:
                return {"status": "NEEDS_RECONCILIATION", "detail": "a batch step failed and rolling back did not fully succeed", "inconsistent_paths": failed, "error": type(exc).__name__}
            return {"status": "CONFLICT", "detail": f"batch not applied (nothing changed): {type(exc).__name__}", "applied": []}
        aid = self._remember(done)
        return {"status": "APPLIED", "files": [d["path"] for d in done], "applied_id": aid}

    # --- undo ---
    def t_propose_undo(self, applied_id):
        steps = self.applied.get(applied_id) if isinstance(applied_id, str) else None
        if steps is None:
            raise ValueError("unknown applied_id; remembered: " + (", ".join(self.applied) or "none (nothing applied in this task yet)"))
        items, diff_parts, files = [], [], []
        for step in reversed(steps):
            path = step["path"]
            if step["op"] == "create":
                current = self.ws.resolve(path).read_bytes()
                if _sha(current) != step["after_sha"]:
                    raise ValueError(f"{path} was changed after it was created; undo refused")
                items.append({"op": "remove", "path": path, "sha": step["after_sha"], "new_bytes": None})
                diff_parts.append(_unified(path, current.decode("utf-8", "replace"), ""))
            elif step["op"] == "remove":
                items.append({"op": "create", "path": path, "sha": None, "new_bytes": step["before"]})
                diff_parts.append(_unified(path, "", step["before"].decode("utf-8", "replace")))
            else:
                current = self.ws.resolve(path).read_bytes()
                if _sha(current) != step["after_sha"]:
                    raise ValueError(f"{path} was changed after that edit; undo refused")
                items.append({"op": "write", "path": path, "sha": step["after_sha"], "new_bytes": step["before"]})
                diff_parts.append(_unified(path, current.decode("utf-8", "replace"), step["before"].decode("utf-8", "replace")))
            files.append({"path": path, "op": items[-1]["op"]})
        return self._propose_write("write_batch", items, {"kind": "undo", "applied_id": applied_id, "files": files}, "".join(diff_parts), f"undo {applied_id}")

    # --- notebooks ---
    def _load_notebook(self, path):
        data = self.ws.resolve(path).read_bytes()
        info = self.flow.inspect(path)
        try:
            text = data.decode("utf-8")
            nb = json.loads(text)
        except (UnicodeError, ValueError):
            raise ValueError("not a valid UTF-8 JSON notebook")
        if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
            raise ValueError("not a Jupyter notebook (no cells list)")
        m = re.search(r"\n( +)\"", text)
        indent = len(m.group(1)) if m else 1
        return nb, text, indent, info["sha256"]

    @staticmethod
    def _cell_source(cell):
        src = cell.get("source", "")
        return "".join(src) if isinstance(src, list) else str(src)

    def t_read_notebook(self, path, start_cell=0, count=20):
        if type(start_cell) is not int or type(count) is not int or start_cell < 0 or not 1 <= count <= 50:
            raise ValueError("invalid start_cell/count")
        nb, _, _, _ = self._load_notebook(path)
        cells = []
        for i, cell in enumerate(nb["cells"][start_cell:start_cell + count], start_cell):
            if not isinstance(cell, dict):
                continue
            source = self._cell_source(cell)
            entry = {"index": i, "cell_type": cell.get("cell_type"), "source": source[:MAX_CELL_PREVIEW]}
            if len(source) > MAX_CELL_PREVIEW:
                entry["source_truncated"] = len(source)
            if cell.get("cell_type") == "code":
                entry["outputs"] = len(cell.get("outputs") or [])
            cells.append(entry)
        return {"path": path, "nbformat": nb.get("nbformat"), "cell_count": len(nb["cells"]), "cells": cells}

    def t_propose_notebook_cell(self, path, index, operation="replace", source=None, cell_type=None):
        if operation not in {"replace", "insert_after", "delete"}:
            raise ValueError("operation must be replace, insert_after or delete")
        nb, text, indent, sha = self._load_notebook(path)
        cells = nb["cells"]
        if type(index) is not int or not (-1 if operation == "insert_after" else 0) <= index < len(cells):
            raise ValueError(f"index out of range (the notebook has {len(cells)} cells; insert_after accepts -1 to insert at the start)")
        if operation != "delete" and not isinstance(source, str):
            raise ValueError("source (a string) is required for replace and insert_after")
        if cell_type is not None and cell_type not in {"code", "markdown", "raw"}:
            raise ValueError("cell_type must be code, markdown or raw")
        lines = source.splitlines(True) if source is not None else []
        if operation == "replace":
            cell = cells[index]
            cell["source"] = lines
            if (cell_type or cell.get("cell_type")) == "code":
                cell["outputs"], cell["execution_count"] = [], None   # outputs of the old source would be stale
            if cell_type:
                cell["cell_type"] = cell_type
        elif operation == "insert_after":
            kind = cell_type or "code"
            new_cell = {"cell_type": kind, "metadata": {}, "source": lines}
            if kind == "code":
                new_cell.update({"execution_count": None, "outputs": []})
            if (nb.get("nbformat"), nb.get("nbformat_minor", 0)) >= (4, 5):
                new_cell["id"] = uuid.uuid4().hex[:8]
            cells.insert(index + 1, new_cell)
        else:
            del cells[index]
        out = json.dumps(nb, indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
        reformatted = out != text and json.dumps(json.loads(text), indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else "") != text
        new_bytes = out.encode("utf-8")
        if len(new_bytes) > MAX_FILE_BYTES:
            raise ValueError("updated notebook too large")
        item = {"op": "write", "path": path, "sha": sha, "new_bytes": new_bytes}
        summary = {"kind": "edit", "path": path, "encoding": "utf-8", "notebook": f"{operation} cell {index}"}
        if reformatted:
            summary["warning"] = "the notebook file is not in standard Jupyter JSON formatting; this change rewrites its layout"
        return self._propose_write("write", [item], summary, _unified(path, text, out), f"notebook {operation} {index}")
