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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import _subprocess
from .messages import ToolDefinition, ToolResult

# ── Tool protocol ─────────────────────────────────────────────────────────────

class Tool(Protocol):
    """Structural type every built-in and custom tool must satisfy.

    A tool is a pure I/O executor: it takes a dict of arguments, performs an
    action against the filesystem or shell, and returns a ``ToolResult``.
    Tools never raise — failures are returned as ``ToolResult(is_error=True)``.

    Required attributes:
        name:        unique identifier; must match the function-call name the
                     LLM emits.
        description: one-line natural-language description shown to the LLM in
                     the tool catalog.
        parameters:  JSON-Schema dict describing accepted arguments.

    Required method:
        execute(args, *, cwd, signal=None) -> ToolResult
            Asynchronously perform the tool's action. ``cwd`` is the sandboxed
            working directory; tools must not access paths outside it. The
            optional ``signal`` is an ``asyncio.Event`` set when the user aborts.

    Provided default:
        definition() builds the ``ToolDefinition`` advertised to the LLM from
        ``name``/``description``/``parameters``. Override only if you need to
        customise the schema.
    """

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def parameters(self) -> dict: ...
    async def execute(self, args: dict, *, cwd: str, signal: asyncio.Event | None = None) -> ToolResult: ...

    def definition(self) -> ToolDefinition:
        """Return the ``ToolDefinition`` advertised to the LLM. Default = name + description + parameters."""
        return ToolDefinition(name=self.name, description=self.description, parameters=self.parameters)

# ── Registry ──────────────────────────────────────────────────────────────────

class ToolRegistry:
    """Mutable registry mapping tool names to ``Tool`` instances.

    Composition roots (``chat.py``, ``autonomous.py``) build a registry via
    ``default_registry()`` and pass it to ``make_config(...)``. The agent loop
    looks up tools by name when the LLM emits a tool call.

    Names must be unique; ``register()`` overwrites any prior tool with the
    same name. Order is insertion order (Python ``dict`` semantics).
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Add (or replace) a tool. Idempotent for the same name."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Return the tool registered under ``name``, or ``None`` if absent."""
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        """Return every registered tool's ``ToolDefinition`` (the LLM-facing schema)."""
        return [t.definition() for t in self._tools.values()]

    def names(self) -> list[str]:
        """Return all registered tool names in insertion order."""
        return list(self._tools.keys())

# ── Shared helpers ────────────────────────────────────────────────────────────

_MAX_OUTPUT = 50 * 1024  # 50 KB hard cap on any tool result
_TRUNCATION_NOTICE = "\n\n[Output truncated — use offset/limit parameters to read more]"

def _cap(text: str) -> str:
    """Cap ``text`` at 50 KB (UTF-8), appending a truncation notice when needed.

    All tools call this on their result before returning. The loop may further
    truncate at append-time (see ``loop._truncate_tool_result``); both layers
    exist so a buggy tool can't blow the context even if loop truncation is
    raised via ``AgentConfig.tool_max_bytes``.
    """
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
    """Execute a shell command in ``cwd`` with a wall-clock timeout.

    Uses ``_subprocess.run`` so the call is async, abort-aware (responds to
    the agent's ``signal`` event), and merges stderr into stdout for one
    captured stream. Output is capped at 50 KB.

    Returns ``ToolResult(is_error=True)`` on non-zero exit, timeout, or abort.
    """

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
            done = await _subprocess.run(
                command, shell=True, cwd=cwd, timeout=timeout,
                signal=signal, merge_stderr=True,
            )
        except asyncio.TimeoutError:
            return ToolResult(content=f"Command timed out after {timeout}s", is_error=True)
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True)
        if done.aborted:
            return ToolResult(content="Command aborted", is_error=True)
        output = done.stdout
        is_err = done.returncode != 0
        if is_err and not output:
            output = f"Exit code {done.returncode}"
        return ToolResult(content=_cap(output), is_error=is_err)

# ── ReadTool ──────────────────────────────────────────────────────────────────

class ReadTool:
    """Read a UTF-8 text file with line numbers, optionally a window.

    Args:
        path:   file path relative to cwd (sandbox-enforced).
        offset: 1-indexed start line (default 1).
        limit:  max lines to return (default 2000).

    Output format: ``"<lineno>\\t<line>"`` per line, plus a trailing
    ``[N more lines — use offset=… to continue]`` hint when truncated.
    """

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
    """Create or overwrite a file with literal ``content``.

    Creates parent directories as needed. For incremental edits to an existing
    file, prefer ``EditTool`` — it preserves unaffected text and validates
    that the target string actually exists.
    """

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
    """Try progressively looser matches for ``old`` inside ``text``.

    Returns ``(matched_old, matched_text)`` where ``matched_old`` is guaranteed
    to be present in ``matched_text``, or ``None`` if no fuzzy match is found.
    Both elements are returned because subsequent edits in a multi-edit batch
    must lookup against the same normalised text.

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
    """Apply one or more ``old_string`` → ``new_string`` replacements atomically.

    Two modes:
        single-edit: pass ``old_string`` / ``new_string`` (+ optional ``replace_all``).
        multi-edit: pass ``edits``, an array of {old_string, new_string, replace_all} objects.

    Semantics (two-phase commit):
        Phase 1 — every ``old_string`` is validated against the **original**
          file content (not the running result). Fuzzy matching auto-handles
          CRLF and trailing-whitespace differences.
        Phase 1.5 — overlap detection: edits whose ``old_strings`` are
          identical (without both being ``replace_all``) or where one contains
          the other are rejected. This prevents silent mis-replacement.
        Phase 2 — replacements are applied sequentially to a working copy and
          the file is rewritten in one ``open("w")`` call.

    Returns ``is_error=True`` on missing path, empty old_string, fuzzy-match
    failure, ambiguous (multi-occurrence without ``replace_all``), or overlap.
    """

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

            # Phase 1.5 — overlap detection: an edit must not target text that
            # is contained within (or contains) the target of another edit.
            # Without this, sequential application silently produces wrong
            # results because the second edit's old_string was consumed/altered
            # by the first.  We forbid the case rather than silently mis-replace.
            for i in range(len(pre_validated)):
                old_i, _, _ = pre_validated[i]
                for j in range(i + 1, len(pre_validated)):
                    old_j, _, _ = pre_validated[j]
                    if old_i == old_j:
                        # Same target string is fine when both have replace_all=True
                        # (they're idempotent); otherwise it's ambiguous.
                        if not (pre_validated[i][2] and pre_validated[j][2]):
                            return ToolResult(
                                content=f"Edits {i+1} and {j+1} target identical old_string — combine them or use replace_all",
                                is_error=True,
                            )
                    elif old_i in old_j or old_j in old_i:
                        return ToolResult(
                            content=f"Edits {i+1} and {j+1} have overlapping old_strings (one contains the other); split into separate calls",
                            is_error=True,
                        )

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
    """Search file contents by regex. Uses ``rg`` if available, else pure-Python re.

    Args:
        pattern:          regex (Python re-flavoured). Required.
        path:             search root, defaults to ``"."``.
        glob:             optional file-glob filter (e.g. ``"**/*.py"``).
        case_insensitive: case-insensitive match.
        context:          lines of context around each match.

    Output: one match per line, ``"<rel-path>:<lineno>: <line>"``. Capped at
    50 KB; capped at 200 files in the Python fallback to bound work.
    """

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
            result = await _subprocess.run(cmd, timeout=30, signal=signal)
            if result.aborted:
                return ToolResult(content="Search aborted", is_error=True)
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
    """List files matching ``pattern`` under ``path``, newest-mtime first.

    Args:
        pattern: glob (e.g. ``"**/*.py"``, ``"src/*.ts"``). Required.
        path:    root directory to search, defaults to ``"."``.

    Returns up to 500 matches; appends ``"... and N more"`` when truncated.
    """

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
    """Return a ``ToolRegistry`` populated with the 6 built-in tools.

    Order: ``Bash`` · ``Read`` · ``Write`` · ``Edit`` · ``Grep`` · ``Find``.
    Composition roots typically call this and then ``register()`` any custom
    tools on top.
    """
    reg = ToolRegistry()
    for tool in [BashTool(), ReadTool(), WriteTool(), EditTool(), GrepTool(), FindTool()]:
        reg.register(tool)
    return reg
