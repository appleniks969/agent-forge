"""
tools.py — Tool protocol, ToolRegistry, and 6 built-in implementations.

Depends only on provider (ToolResult, ToolDefinition). No LLM calls, no session
state, no context logic — tools are pure I/O executors. The loop imports this to
execute tool calls; nothing below the loop needs to know tools exist.

Owns: Tool (Protocol), ToolRegistry, default_registry(), BashTool, ReadTool,
      WriteTool, EditTool, GrepTool, FindTool, path sandboxing (_sandbox),
      50 KB output cap (_cap).
All tools: never raise — always return ToolResult(is_error=True) on failure.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .messages import ToolDefinition, ToolResult

# ── Tool protocol ─────────────────────────────────────────────────────────────

class Tool(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters(self) -> dict: ...
    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult: ...

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

# ── Registry ──────────────────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [t.definition() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

# ── Shared helpers ────────────────────────────────────────────────────────────

_MAX_OUTPUT = 50 * 1024  # 50 KB hard cap on any tool result
_TRUNCATION_NOTICE = "\n\n[Output truncated — use offset/limit parameters to read more]"

def _cap(text: str) -> str:
    if len(text.encode()) <= _MAX_OUTPUT:
        return text
    # Truncate to byte limit preserving valid UTF-8
    encoded = text.encode()[:_MAX_OUTPUT]
    return encoded.decode("utf-8", errors="ignore") + _TRUNCATION_NOTICE

def _sandbox(path: str, cwd: str) -> str:
    """Resolve path relative to cwd; reject traversal above cwd."""
    resolved = Path(os.path.join(cwd, path)).resolve()
    cwd_resolved = Path(cwd).resolve()
    try:
        resolved.relative_to(cwd_resolved)
    except ValueError:
        raise ValueError(f"Path {path!r} escapes working directory")
    return str(resolved)

# ── BashTool ──────────────────────────────────────────────────────────────────

class BashTool:
    name = "Bash"
    description = (
        "Execute a shell command in the working directory. "
        "Use for running tests, build commands, git operations, or any shell task. "
        "Avoid interactive commands. Timeout: 120s."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)", "default": 120},
        },
        "required": ["command"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        command = args.get("command", "")
        timeout = int(args.get("timeout", 120))
        if not command:
            return ToolResult(content="Error: no command provided", is_error=True)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(content=f"Command timed out after {timeout}s", is_error=True)
            output = stdout.decode("utf-8", errors="replace")
            is_err = (proc.returncode or 0) != 0
            if is_err and not output:
                output = f"Exit code {proc.returncode}"
            return ToolResult(content=_cap(output), is_error=is_err)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)

# ── ReadTool ──────────────────────────────────────────────────────────────────

class ReadTool:
    name = "Read"
    description = (
        "Read a file's contents with line numbers. "
        "Use offset and limit to read large files in chunks. "
        "Default: up to 2000 lines from the start."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to cwd)"},
            "offset": {"type": "integer", "description": "Start line (1-indexed)", "default": 1},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        path = args.get("path", "")
        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, int(args.get("limit", 2000)))
        if not path:
            return ToolResult(content="Error: no path provided", is_error=True)
        try:
            resolved = _sandbox(path, cwd)
            with open(resolved, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total = len(lines)
            start = offset - 1
            end = min(start + limit, total)
            chunk = lines[start:end]
            numbered = "".join(f"{start + i + 1}\t{line}" for i, line in enumerate(chunk))
            if end < total:
                numbered += f"\n[{total - end} more lines — use offset={end + 1} to continue]"
            return ToolResult(content=_cap(numbered))
        except FileNotFoundError:
            return ToolResult(content=f"File not found: {path}", is_error=True)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)

# ── WriteTool ─────────────────────────────────────────────────────────────────

class WriteTool:
    name = "Write"
    description = (
        "Write content to a file (creates or overwrites). "
        "Use for new files or complete rewrites. "
        "For targeted edits to existing files, prefer Edit."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to cwd)"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return ToolResult(content="Error: no path provided", is_error=True)
        try:
            resolved = _sandbox(path, cwd)
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count("\n") + (1 if content else 0)
            return ToolResult(content=f"Wrote {lines} line(s) to {path}")
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)

# ── EditTool helpers (Fix 2: fuzzy matching) ────────────────────────────────

def _fuzzy_find(text: str, old: str) -> "tuple[str, str] | None":
    """
    Try progressively looser matches for old_string.
    Returns (matched_old, matched_text) where matched_old is guaranteed to be
    present in matched_text, or None if no fuzzy match found.

    Strategies (in order):
      1. CRLF -> LF normalisation (Windows line endings in file or old_string)
      2. Strip trailing whitespace per line (most common editor divergence)
    """
    # 1. CRLF -> LF
    old_lf  = old.replace("\r\n", "\n")
    text_lf = text.replace("\r\n", "\n")
    if old_lf in text_lf:
        return old_lf, text_lf
    # 2. Strip trailing whitespace per line
    def _rstrip_lines(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.splitlines())
    old_rs  = _rstrip_lines(old_lf)
    text_rs = _rstrip_lines(text_lf)
    if old_rs and old_rs in text_rs:
        return old_rs, text_rs
    return None


# ── EditTool (Fix 1: multi-edit batching, Fix 2: fuzzy matching) ──────────────

class EditTool:
    name = "Edit"
    description = (
        "Make one or more targeted edits to an existing file. "
        "Supply EITHER a single old_string/new_string pair OR an edits array for "
        "multiple replacements in one atomic call. "
        "Each old_string is matched against the original file content (not the running result). "
        "Read the file first to get the exact text. "
        "Use replace_all=true to replace every occurrence of a given old_string. "
        "Fuzzy matching handles trailing-whitespace and CRLF differences automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (relative to cwd)"},
            "old_string": {"type": "string", "description": "Exact text to replace (single-edit mode)"},
            "new_string": {"type": "string", "description": "Replacement text (single-edit mode)"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (single-edit mode)", "default": False},
            "edits": {
                "type": "array",
                "description": "Batch of edits applied atomically (multi-edit mode). Each old_string matched against original file.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Exact text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        path = args.get("path", "")
        if not path:
            return ToolResult(content="Error: no path provided", is_error=True)

        # Fix 1: normalise to a list of (old, new, replace_all) triples
        edits_raw = args.get("edits")
        if edits_raw:
            edits: list[tuple[str, str, bool]] = [
                (e["old_string"], e["new_string"], bool(e.get("replace_all", False)))
                for e in edits_raw
            ]
        elif args.get("old_string") is not None:
            edits = [(args["old_string"], args.get("new_string", ""), bool(args.get("replace_all", False)))]
        else:
            return ToolResult(content="Error: provide old_string or edits array", is_error=True)

        try:
            resolved = _sandbox(path, cwd)
            with open(resolved, encoding="utf-8") as f:
                original = f.read()

            # Fix 1 (original-based matching, two-phase):
            # Phase 1 — validate EVERY old_string against the original file before
            #   changing anything.  This matches coding-agent-flow semantics: you cannot
            #   reference text that only exists after a previous edit in the same batch.
            #   Fuzzy normalisation (CRLF / trailing-ws) is propagated forward so that
            #   subsequent lookups work in the same normalised space.
            pre_validated: list[tuple[str, str, bool]] = []
            check_text = original  # accumulates normalisation; never modified by new_strings
            for idx, (old, new, replace_all) in enumerate(edits):
                if not old:
                    return ToolResult(content="Error: old_string must not be empty", is_error=True)
                if old not in check_text:
                    # Fix 2: fuzzy match (CRLF + trailing whitespace) against original
                    match = _fuzzy_find(check_text, old)
                    if match is None:
                        ctx = f" (edit {idx+1}/{len(edits)})" if len(edits) > 1 else ""
                        return ToolResult(
                            content=f"old_string not found in {path}{ctx}; fuzzy match also failed",
                            is_error=True,
                        )
                    old, check_text = match  # propagate normalisation for remaining lookups
                count = check_text.count(old)
                if count > 1 and not replace_all:
                    return ToolResult(
                        content=f"old_string appears {count} times in {path} - use replace_all=true or add more context",
                        is_error=True,
                    )
                pre_validated.append((old, new, replace_all))

            # Phase 2 — apply sequentially to a working copy of the (possibly
            #   normalised) original.  old_strings reference pre-edit text only.
            working = check_text
            result_lines: list[str] = []
            for old, new, replace_all in pre_validated:
                replaced = working.count(old) if replace_all else 1
                working = working.replace(old, new) if replace_all else working.replace(old, new, 1)
                result_lines.append(f"Replaced {replaced} occurrence(s)")

            with open(resolved, "w", encoding="utf-8") as f:
                f.write(working)

            if len(edits) == 1:
                return ToolResult(content=f"{result_lines[0]} in {path}")
            return ToolResult(content=f"Applied {len(edits)} edits to {path}: " + "; ".join(result_lines))

        except FileNotFoundError:
            return ToolResult(content=f"File not found: {path}", is_error=True)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)
# ── GrepTool ──────────────────────────────────────────────────────────────────

class GrepTool:
    name = "Grep"
    description = (
        "Search file contents using a regex pattern. "
        "Returns matching lines with file:line format. "
        "Use glob to filter files (e.g. '**/*.py')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory or file path to search in", "default": "."},
            "glob": {"type": "string", "description": "File glob filter (e.g. '**/*.py')"},
            "case_insensitive": {"type": "boolean", "description": "Case-insensitive search", "default": False},
            "context": {"type": "integer", "description": "Lines of context around matches", "default": 0},
        },
        "required": ["pattern"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        file_glob = args.get("glob", "")
        case_insensitive = bool(args.get("case_insensitive", False))
        context_lines = int(args.get("context", 0))
        if not pattern:
            return ToolResult(content="Error: no pattern provided", is_error=True)

        # Try rg first (faster), fall back to Python re
        try:
            cmd = ["rg", "--line-number", "--no-heading"]
            if case_insensitive:
                cmd.append("-i")
            if context_lines > 0:
                cmd += ["-C", str(context_lines)]
            if file_glob:
                cmd += ["--glob", file_glob]
            cmd += [pattern, os.path.join(cwd, path)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode not in (0, 1):  # 1 = no matches (not an error)
                raise OSError("rg failed")
            return ToolResult(content=_cap(result.stdout) if result.stdout else "(no matches)")
        except (FileNotFoundError, OSError):
            pass

        # Python fallback
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            compiled = re.compile(pattern, flags)
            search_root = Path(os.path.join(cwd, path)).resolve()
            if file_glob:
                files = list(search_root.glob(file_glob)) if search_root.is_dir() else [search_root]
            elif search_root.is_file():
                files = [search_root]
            else:
                files = [p for p in search_root.rglob("*") if p.is_file()]

            lines_out: list[str] = []
            for fp in sorted(files)[:200]:
                try:
                    text_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, line in enumerate(text_lines, 1):
                        if compiled.search(line):
                            rel = os.path.relpath(fp, cwd)
                            lines_out.append(f"{rel}:{i}: {line}")
                except Exception:
                    continue
            if not lines_out:
                return ToolResult(content="(no matches)")
            return ToolResult(content=_cap("\n".join(lines_out)))
        except re.error as exc:
            return ToolResult(content=f"Invalid regex: {exc}", is_error=True)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)

# ── FindTool ──────────────────────────────────────────────────────────────────

class FindTool:
    name = "Find"
    description = (
        "Find files matching a glob pattern. "
        "Returns file paths sorted by modification time (newest first). "
        "Use path to limit search scope."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'src/*.ts')"},
            "path": {"type": "string", "description": "Root directory to search in", "default": "."},
        },
        "required": ["pattern"],
    }

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        if not pattern:
            return ToolResult(content="Error: no pattern provided", is_error=True)
        try:
            search_root = Path(os.path.join(cwd, path)).resolve()
            matches = list(search_root.glob(pattern))
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if not matches:
                return ToolResult(content="(no files found)")
            lines = [os.path.relpath(m, cwd) for m in matches[:500]]
            result = "\n".join(lines)
            if len(matches) > 500:
                result += f"\n... and {len(matches) - 500} more"
            return ToolResult(content=result)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)


# ── Default registry ──────────────────────────────────────────────────────────

def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in [BashTool(), ReadTool(), WriteTool(), EditTool(), GrepTool(), FindTool()]:
        reg.register(tool)
    return reg
