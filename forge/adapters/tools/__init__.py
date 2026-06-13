"""adapters.tools: the six built-in tools plus the Workspace implementation.

Layer: adapters — imports ports + kernel only. Convention shared by every
built-in: tools never raise (errors are ToolResult(is_error=True)) and they
return call_id="" — the executor stamps the real call id on the way out.
Import classes from their defining modules (workspace, proc, bash, fs,
search); this package exposes only the composition helper builtin_tools().
"""

from __future__ import annotations

from forge.adapters.tools.bash import BashTool
from forge.adapters.tools.fs import EditTool, ReadTool, WriteTool
from forge.adapters.tools.search import FindTool, GrepTool
from forge.ports.tool import Tool


def builtin_tools() -> tuple[Tool, ...]:
    return (BashTool(), ReadTool(), WriteTool(), EditTool(), GrepTool(), FindTool())
