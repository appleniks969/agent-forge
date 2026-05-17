"""
agent_forge.wiki.gather — pull repo signal into .agent-forge/raw/.

Public entry points:
  run_gather(repo_root, since=None, only=None) -> GatherResult
  build_parser(subparsers) -> registers the `wiki` subcommand
"""
from __future__ import annotations

from .cli import build_parser
from .discovery import run_gather

__all__ = ["run_gather", "build_parser"]
