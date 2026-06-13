"""Permission policy: pure guard predicates and the ordered chain combinator.

Layer: policy — pure and synchronous, imports kernel only.
judge(call, effects, ws_root) -> Allow | Deny(reason) | Ask(question).

Guards key on Effects flags and argument shapes — never on tool-name strings —
so renaming a tool cannot silently disarm its guard. Every guard in a chain
runs on every call (audit semantics: all guards observe); the first Deny wins,
then the first Ask; otherwise Allow. The destructive-bash pattern set is the
floor, not the ceiling — heuristics, honestly labeled.
"""

from __future__ import annotations

import os.path
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePath

from forge.kernel.types import (
    Allow,
    Ask,
    Deny,
    Effects,
    PermissionQuestion,
    ToolCall,
    Verdict,
)

# A guard returns None to abstain; a Verdict to weigh in.
Guard = Callable[[ToolCall, Effects, PurePath], "Verdict | None"]


@dataclass(frozen=True)
class GuardChain:
    guards: tuple[Guard, ...]

    def judge(self, call: ToolCall, effects: Effects, ws_root: PurePath) -> Verdict:
        verdicts = tuple(guard(call, effects, ws_root) for guard in self.guards)
        for verdict in verdicts:
            if isinstance(verdict, Deny):
                return verdict
        for verdict in verdicts:
            if isinstance(verdict, Ask):
                return verdict
        return Allow()


def chain(*guards: Guard) -> GuardChain:
    return GuardChain(tuple(guards))


# --- standard guards --------------------------------------------------------------


def external_guard(
    call: ToolCall, effects: Effects, ws_root: PurePath
) -> Verdict | None:
    """EXTERNAL effects => default verdict is Ask: we do not auto-allow tools
    that act beyond the workspace on their own self-description."""
    if Effects.EXTERNAL in effects:
        return Ask(
            PermissionQuestion(
                call_id=call.id,
                tool=call.name,
                question=(
                    f"{call.name} declares EXTERNAL effects (acts beyond the "
                    "workspace); allow this call?"
                ),
            )
        )
    return None


_COMMAND_KEYS = ("command", "cmd", "script")
_BASH_DENY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsudo\b"), "sudo is not allowed"),
    (
        re.compile(r"\brm\s+(?:-\w+\s+)*-\w*[rf]\w*\s+(?:/|\$HOME|~)(?:\s|$)"),
        "rm -rf on a system or home path",
    ),
    (re.compile(r"\bgit\s+push\s+(?:\S+\s+)*(?:-f|--force)"), "force-push to remote"),
    (re.compile(r"\bgit\s+reset\s+--hard\s+origin/"), "hard reset against remote"),
    (re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:"), "fork bomb"),
)


def destructive_bash_guard(
    call: ToolCall, effects: Effects, ws_root: PurePath
) -> Verdict | None:
    """Heuristic floor over EXEC tools' command-shaped args."""
    if Effects.EXEC not in effects:
        return None
    command = " ".join(
        value
        for key in _COMMAND_KEYS
        if isinstance(value := call.args.get(key), str)
    )
    if not command:
        return None
    for pattern, reason in _BASH_DENY:
        if pattern.search(command):
            return Deny(reason)
    return None


# Workspace containment is the executor's job; this is the second wall: even a
# path legitimately reachable (absolute, or via ~) must not hit these targets.
_PATH_KEYS = (
    "path",
    "file_path",
    "file",
    "dest",
    "destination",
    "target",
    "dir",
    "directory",
)
_DENY_PREFIXES = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc")
_DENY_SEGMENTS = frozenset({".ssh", ".aws", ".gnupg"})


def sensitive_path_guard(
    call: ToolCall, effects: Effects, ws_root: PurePath
) -> Verdict | None:
    if Effects.WRITE_PATH not in effects:
        return None
    for key in _PATH_KEYS:
        value = call.args.get(key)
        if not isinstance(value, str) or not value:
            continue
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(ws_root), expanded)
        normalized = os.path.normpath(expanded)
        for prefix in _DENY_PREFIXES:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return Deny(f"write to {prefix} is denied")
        for segment in normalized.split(os.sep):
            if segment in _DENY_SEGMENTS:
                return Deny(f"write into {segment} is denied")
    return None


STANDARD_GUARDS: tuple[Guard, ...] = (
    destructive_bash_guard,
    sensitive_path_guard,
    external_guard,
)

_STANDARD_CHAIN = GuardChain(STANDARD_GUARDS)


def judge(call: ToolCall, effects: Effects, ws_root: PurePath) -> Verdict:
    """Judge one call with the standard guard set."""
    return _STANDARD_CHAIN.judge(call, effects, ws_root)


def guards_from(extra: Sequence[Guard]) -> GuardChain:
    """Standard guards first (their Deny floor cannot be bypassed), extras after."""
    return GuardChain(STANDARD_GUARDS + tuple(extra))
