"""GrepTool and FindTool: content and filename search under the Workspace.

Layer: adapters/tools. Both prefer the fast binaries (rg, fd) when installed
and fall back to pure Python otherwise. Parity is scoped to trees without
ignore files: both engines skip hidden entries and symlinks, but only the
binaries honor .gitignore. Never raises — errors are ToolResults with
call_id="" for the executor to stamp.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from forge.adapters.tools import proc
from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx, WorkspaceEscape

_SEARCH_TIMEOUT = 30.0
_MAX_FILES = 1000  # bound on fallback grep work, mirrors the legacy file cap
_MAX_FIND = 500


def _err(msg: str) -> ToolResult:
    return ToolResult(call_id="", content=f"Error: {msg}", is_error=True)


def _ok(content: str) -> ToolResult:
    return ToolResult(call_id="", content=content)


def _hidden(p: Path, base: Path) -> bool:
    return any(part.startswith(".") for part in p.relative_to(base).parts)


class GrepTool:
    spec = ToolSpec(
        name="Grep",
        description=(
            "Search file contents with a regex pattern. Returns matches in "
            "path:line:text format. Use glob to filter files "
            "(e.g. '**/*.py')."
        ),
        params={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "format": "path",
                    "description": "Directory or file to search",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '**/*.py')",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search",
                    "default": False,
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context around matches (rg only)",
                    "default": 0,
                },
            },
            "required": ["pattern"],
        },
        effects=Effects.READ_PATH,
    )

    def __init__(self, use_rg: bool | None = None) -> None:
        # None = autodetect; tests force each engine to assert parity.
        self._use_rg = use_rg

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return await self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    async def _execute(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return _err("no pattern provided")
        path = str(args.get("path") or ".")
        try:
            target = ctx.ws.resolve(path)
        except WorkspaceEscape as exc:
            return _err(str(exc))
        if not target.exists():
            return _err(f"path not found: {path}")
        file_glob = str(args.get("glob") or "")
        ci = bool(args.get("case_insensitive", False))
        context = int(args.get("context") or 0)
        use_rg = self._use_rg if self._use_rg is not None else shutil.which("rg") is not None
        if use_rg:
            result = await self._rg(pattern, target, file_glob, ci, context, ctx)
            if result is not None:
                return result
        if ctx.cancel.is_set():
            return _err("search aborted")
        return self._fallback(pattern, target, file_glob, ci, ctx)

    async def _rg(
        self,
        pattern: str,
        target: Path,
        file_glob: str,
        ci: bool,
        context: int,
        ctx: ToolCtx,
    ) -> ToolResult | None:
        # -H forces path:line:text even for a single-file target, keeping the
        # output shape identical to the fallback's.
        cmd = ["rg", "--line-number", "--no-heading", "--with-filename", "--color", "never"]
        if ci:
            cmd.append("-i")
        if context > 0:
            cmd += ["-C", str(context)]
        if file_glob:
            cmd += ["--glob", file_glob]
        cmd += ["--", pattern]
        rel = target.relative_to(ctx.ws.root)
        if rel != Path("."):
            cmd.append(str(rel))
        try:
            done = await proc.run(
                cmd, cwd=str(ctx.ws.root), timeout=_SEARCH_TIMEOUT, cancel=ctx.cancel
            )
        except FileNotFoundError:
            return None  # rg not installed after all
        except TimeoutError:
            return _err(f"search timed out after {_SEARCH_TIMEOUT:g}s")
        if done.aborted:
            return _err("search aborted")
        if done.returncode == 0:
            return _ok(done.stdout.rstrip("\n") or "(no matches)")
        if done.returncode == 1:  # rg: no matches, not an error
            return _ok("(no matches)")
        return None  # rg error (e.g. regex dialect mismatch): let the fallback try

    def _fallback(
        self, pattern: str, target: Path, file_glob: str, ci: bool, ctx: ToolCtx
    ) -> ToolResult:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ci else 0)
        except re.error as exc:
            return _err(f"invalid regex: {exc}")
        if target.is_file():
            files = [target]
        elif file_glob:
            files = [
                p
                for p in target.glob(file_glob)
                if p.is_file() and not p.is_symlink() and not _hidden(p, target)
            ]
        else:
            files = [
                p
                for p in target.rglob("*")
                if p.is_file() and not p.is_symlink() and not _hidden(p, target)
            ]
        out: list[str] = []
        for fp in sorted(files)[:_MAX_FILES]:
            if ctx.cancel.is_set():
                return _err("search aborted")
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "\x00" in text:  # binary, which rg also skips
                continue
            rel = fp.relative_to(ctx.ws.root)
            for i, line in enumerate(text.splitlines(), 1):
                if compiled.search(line):
                    out.append(f"{rel}:{i}:{line}")
        return _ok("\n".join(out) if out else "(no matches)")


def _fd_name_glob(pattern: str) -> str | None:
    """Translate a pathlib glob into an fd filename glob, or None.

    fd matches its glob against the filename at any depth, which equals
    pathlib's "**/<name>" (zero or more directories). Anything else has no
    exact fd equivalent and uses the python path.
    """
    if pattern.startswith("**/"):
        tail = pattern[3:]
        if tail and "/" not in tail and "**" not in tail:
            return tail
    return None


class FindTool:
    spec = ToolSpec(
        name="Find",
        description=(
            "Find files matching a glob pattern. Returns paths sorted by "
            "modification time (newest first). Use path to limit the search "
            "scope."
        ),
        params={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/*.ts')",
                },
                "path": {
                    "type": "string",
                    "format": "path",
                    "description": "Root directory to search in",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
        effects=Effects.READ_PATH,
    )

    def __init__(self, use_fd: bool | None = None) -> None:
        self._use_fd = use_fd

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return await self._execute(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    async def _execute(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return _err("no pattern provided")
        path = str(args.get("path") or ".")
        try:
            target = ctx.ws.resolve(path)
        except WorkspaceEscape as exc:
            return _err(str(exc))
        if not target.is_dir():
            return _err(f"not a directory: {path}")
        matches: list[Path] | None = None
        name_glob = _fd_name_glob(pattern)
        use_fd = self._use_fd if self._use_fd is not None else shutil.which("fd") is not None
        if use_fd and name_glob is not None:
            matches = await self._fd(name_glob, target, ctx)
        if ctx.cancel.is_set():
            return _err("search aborted")
        if matches is None:
            matches = [p for p in target.glob(pattern) if not _hidden(p, target)]
        entries: list[tuple[float, Path]] = []
        for p in matches:
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue
        if not entries:
            return _ok("(no files found)")
        entries.sort(key=lambda e: (-e[0], str(e[1])))
        rels = [str(p.relative_to(ctx.ws.root)) for _, p in entries[:_MAX_FIND]]
        content = "\n".join(rels)
        if len(entries) > _MAX_FIND:
            content += f"\n... and {len(entries) - _MAX_FIND} more"
        return _ok(content)

    async def _fd(self, name_glob: str, target: Path, ctx: ToolCtx) -> list[Path] | None:
        cmd = ["fd", "--glob", "--color", "never", "--", name_glob]
        try:
            done = await proc.run(
                cmd, cwd=str(target), timeout=_SEARCH_TIMEOUT, cancel=ctx.cancel
            )
        except (FileNotFoundError, TimeoutError):
            return None
        if done.aborted or done.returncode != 0:
            return None
        return [target / line for line in done.stdout.splitlines() if line]
