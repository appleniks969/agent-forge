"""ReadTool, WriteTool, EditTool: file I/O under the Workspace authority.

Layer: adapters/tools. Every path goes through ctx.ws.resolve — the executor
also pre-resolves fields marked format:"path", so containment is belt and
suspenders. Output caps live in the executor (one knob), not here. Tools
never raise; errors come back as ToolResult(is_error=True) with call_id=""
for the executor to stamp.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx, WorkspaceEscape


def _err(msg: str) -> ToolResult:
    return ToolResult(call_id="", content=f"Error: {msg}", is_error=True)


def _ok(content: str) -> ToolResult:
    return ToolResult(call_id="", content=content)


class ReadTool:
    spec = ToolSpec(
        name="Read",
        description=(
            "Read a file's contents with line numbers. Use offset and limit "
            "to read large files in chunks. Default: up to 2000 lines from "
            "the start."
        ),
        params={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "format": "path",
                    "description": "File path (relative to the workspace root)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start line (1-indexed)",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read",
                    "default": 2000,
                },
            },
            "required": ["path"],
        },
        effects=Effects.READ_PATH,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    def _execute(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        path = str(args.get("path") or "")
        if not path:
            return _err("no path provided")
        try:
            offset = max(1, int(args.get("offset") or 1))
            limit = max(1, int(args.get("limit") or 2000))
        except (TypeError, ValueError):
            return _err("offset and limit must be integers")
        try:
            resolved = ctx.ws.resolve(path)
        except WorkspaceEscape as exc:
            return _err(str(exc))
        try:
            with open(resolved, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return _err(f"file not found: {path}")
        except IsADirectoryError:
            return _err(f"is a directory: {path}")
        total = len(lines)
        start = offset - 1
        end = min(start + limit, total)
        numbered = "".join(
            f"{start + i + 1}\t{line}" for i, line in enumerate(lines[start:end])
        )
        if end < total:
            numbered += f"\n[{total - end} more lines — use offset={end + 1} to continue]"
        return _ok(numbered)


class WriteTool:
    spec = ToolSpec(
        name="Write",
        description=(
            "Write content to a file (creates or overwrites; parent "
            "directories are created as needed). Use for new files or "
            "complete rewrites. For targeted edits to existing files, "
            "prefer Edit."
        ),
        params={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "format": "path",
                    "description": "File path (relative to the workspace root)",
                },
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
        effects=Effects.WRITE_PATH,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    def _execute(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        path = str(args.get("path") or "")
        if not path:
            return _err("no path provided")
        content = args.get("content")
        if not isinstance(content, str):
            return _err("content must be a string")
        try:
            resolved = ctx.ws.resolve(path)
        except WorkspaceEscape as exc:
            return _err(str(exc))
        if ctx.cancel.is_set():  # do not mutate after the user aborted
            return _err("aborted")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        lines = content.count("\n") + (1 if content else 0)
        return _ok(f"Wrote {lines} line(s) to {path}")


class EditTool:
    spec = ToolSpec(
        name="Edit",
        description=(
            "Make targeted edits to an existing file by exact string match. "
            "Supply EITHER a single old_string/new_string pair OR an edits "
            "array for several replacements in one atomic call. Each "
            "old_string is matched against the original file content and "
            "must occur exactly once unless replace_all is true. Read the "
            "file first to get the exact text."
        ),
        params={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "format": "path",
                    "description": "File path (relative to the workspace root)",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace (single-edit mode)",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text (single-edit mode)",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence (single-edit mode)",
                    "default": False,
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "Batch of edits applied atomically (multi-edit mode); "
                        "each old_string is matched against the original file"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                            "replace_all": {"type": "boolean", "default": False},
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path"],
        },
        effects=Effects.READ_PATH | Effects.WRITE_PATH,
    )

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    def _execute(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        path = str(args.get("path") or "")
        if not path:
            return _err("no path provided")
        edits_raw = args.get("edits")
        if edits_raw:
            try:
                edits = [
                    (
                        str(e["old_string"]),
                        str(e.get("new_string", "")),
                        bool(e.get("replace_all", False)),
                    )
                    for e in edits_raw
                ]
            except (TypeError, KeyError):
                return _err("each edit needs old_string and new_string")
        elif args.get("old_string") is not None:
            edits = [
                (
                    str(args["old_string"]),
                    str(args.get("new_string", "")),
                    bool(args.get("replace_all", False)),
                )
            ]
        else:
            return _err("provide old_string or an edits array")
        try:
            resolved = ctx.ws.resolve(path)
        except WorkspaceEscape as exc:
            return _err(str(exc))
        try:
            original = resolved.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _err(f"file not found: {path}")
        except IsADirectoryError:
            return _err(f"is a directory: {path}")

        # Phase 1: every old_string is validated against the ORIGINAL text —
        # an edit cannot reference text that only exists after a previous
        # edit in the same batch.
        for idx, (old, _new, replace_all) in enumerate(edits):
            label = f" (edit {idx + 1}/{len(edits)})" if len(edits) > 1 else ""
            if not old:
                return _err(f"old_string must not be empty{label}")
            count = original.count(old)
            if count == 0:
                return _err(f"old_string not found in {path}{label}")
            if count > 1 and not replace_all:
                return _err(
                    f"old_string appears {count} times in {path}{label} — "
                    "use replace_all=true or add more context"
                )

        # Phase 1.5: overlap detection — sequential application would
        # silently mis-replace when one old_string is consumed or altered by
        # another, so the ambiguous cases are forbidden outright.
        for i in range(len(edits)):
            for j in range(i + 1, len(edits)):
                old_i, old_j = edits[i][0], edits[j][0]
                if old_i == old_j:
                    # Identical targets are idempotent only when both replace_all.
                    if not (edits[i][2] and edits[j][2]):
                        return _err(
                            f"edits {i + 1} and {j + 1} target identical "
                            "old_string — combine them or use replace_all"
                        )
                elif old_i in old_j or old_j in old_i:
                    return _err(
                        f"edits {i + 1} and {j + 1} have overlapping "
                        "old_strings (one contains the other) — split into "
                        "separate calls"
                    )

        if ctx.cancel.is_set():  # do not mutate after the user aborted
            return _err("aborted")

        working = original
        notes: list[str] = []
        for old, new, replace_all in edits:
            n = working.count(old) if replace_all else 1
            working = working.replace(old, new) if replace_all else working.replace(old, new, 1)
            notes.append(f"Replaced {n} occurrence(s)")
        resolved.write_text(working, encoding="utf-8")

        if len(edits) == 1:
            return _ok(f"{notes[0]} in {path}")
        return _ok(f"Applied {len(edits)} edits to {path}: " + "; ".join(notes))
