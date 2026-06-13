"""Asker port: resolves Ask verdicts.

Layer: ports — Protocols over kernel types only, no logic. The REPL prompts
the human; oneshot applies a policy file. True means allow.
"""

from __future__ import annotations

from typing import Protocol

from forge.kernel.types import PermissionQuestion


class Asker(Protocol):
    async def ask(self, question: PermissionQuestion) -> bool: ...
