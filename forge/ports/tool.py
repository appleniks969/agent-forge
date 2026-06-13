"""Tool and Workspace ports.

Layer: ports — Protocols over kernel types only, no logic. Tools never raise:
they return an error ToolResult. The Workspace is the only path authority;
resolve() raises WorkspaceEscape for anything outside the root, and the
executor resolves every format:"path" schema field through it before run().
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from forge.kernel.types import ToolResult, ToolSpec

if TYPE_CHECKING:
    import asyncio


class WorkspaceEscape(Exception):
    """Raised by Workspace.resolve for paths outside the workspace root."""


class Workspace(Protocol):
    @property
    def root(self) -> Path: ...

    def resolve(self, path: str) -> Path: ...


@dataclass(frozen=True)
class ToolCtx:
    ws: Workspace
    cancel: asyncio.Event


class Tool(Protocol):
    spec: ToolSpec

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult: ...
