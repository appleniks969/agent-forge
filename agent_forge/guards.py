"""
guards.py — reusable safety hooks (Hooks Protocol implementations).

Depends only on hooks (NoopHooks, HookDecision) and messages (ToolCallContent).
Sits one notch above hooks.py — any composition root can opt in by passing
``AgentRuntime(hooks=_CompositeHook(...))`` or ``make_config(hooks=...)``.

These hooks were originally tied to the autonomous worktree pipeline (now
removed) but the policies they encode — block destructive shell commands,
block writes to system paths, block destructive MCP verbs — are useful in
any non-interactive or "safe-mode" composition root.

Owns:
  - BashGuardHook   — vetoes sudo, rm -rf /, force-push, hard-reset, fork bombs.
  - PathGuardHook   — vetoes Write / Edit to a deny-list of system paths.
  - MCPGuardHook    — vetoes MCP tool calls whose namespaced name contains a
                       destructive verb (delete, drop, purge, …).
  - _CompositeHook  — chains multiple Hooks with strict-contract semantics.
"""
from __future__ import annotations

import os
import re

from .hooks import HookDecision, NoopHooks
from .messages import ToolCallContent


# ── BashGuardHook ─────────────────────────────────────────────────────────────
#
# Block destructive Bash commands. Useful in non-interactive composition roots
# where the agent has no human in the loop to confirm. Pattern set is
# intentionally small — it's the floor, not the ceiling.

class BashGuardHook(NoopHooks):
    """Veto destructive Bash commands. Returns None for everything else."""

    _BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bsudo\b"),                            "sudo is not allowed"),
        (re.compile(r"\brm\s+-rf?\s+(/|\$HOME|~)(\s|$)"),    "rm -rf on system / home path"),
        (re.compile(r"\bgit\s+push\s+(\S+\s+)*(-f|--force)"), "force-push to remote"),
        (re.compile(r"\bgit\s+reset\s+--hard\s+origin/"),    "hard reset against remote"),
        (re.compile(r":\(\)\s*\{[^}]*\}\s*;\s*:"),           "fork bomb"),
    ]

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        if call.name != "Bash":
            return None
        cmd = str(call.arguments.get("command", ""))
        for pattern, reason in self._BLOCK_PATTERNS:
            if pattern.search(cmd):
                return HookDecision(block=True, reason=reason)
        return None


# ── PathGuardHook ─────────────────────────────────────────────────────────────
#
# Veto file writes (Write / Edit) targeting sensitive system locations even
# when the path expression slips past tools._sandbox() (e.g. via tilde or
# absolute paths invoked with --cwd /). This is the second wall: tools.py
# enforces "must be inside cwd"; PathGuardHook enforces "even if you are
# inside cwd, certain paths are off-limits".

class PathGuardHook(NoopHooks):
    """Veto Write / Edit to sensitive paths. Configurable deny-list."""

    _DEFAULT_DENY = (
        "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc",
        "/.ssh", "/.aws", "/.gnupg",
    )

    def __init__(self, deny_paths: tuple[str, ...] | None = None) -> None:
        self._deny = deny_paths if deny_paths is not None else self._DEFAULT_DENY

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        if call.name not in ("Write", "Edit"):
            return None
        path = str(call.arguments.get("path", ""))
        if not path:
            return None
        # Resolve user-relative shortcuts before matching. Don't resolve fully
        # (the agent may not have file-system access yet) — just expand ~.
        expanded = os.path.expanduser(path)
        for needle in self._deny:
            if needle in expanded:
                return HookDecision(block=True, reason=f"write to {needle} is denied")
        return None


# ── MCPGuardHook ──────────────────────────────────────────────────────────────
#
# Block destructive MCP tool calls. MCP tools execute inside their server's
# process, so a `github__delete_repo` or `db__drop_table` can do real,
# unrecoverable damage. Heuristic: a small set of "destructive verbs" that
# appear as tokens in the tool's namespaced name. The list is the floor —
# callers can extend it via `extra_verbs` / `extra_prefixes`.

class MCPGuardHook(NoopHooks):
    """Veto MCP tool calls whose name suggests a destructive action.

    Detection is by tool-name *tokens* (we can't introspect the server's
    intent). The tool half of a namespaced name is split on ``_`` /
    ``-`` and matched against:
      • ``_DEFAULT_VERBS``  — exact-token match (``delete_repo`` blocks)
      • ``_DEFAULT_PREFIXES`` — startswith match (``force_push`` blocks)

    Pass ``extra_verbs`` / ``extra_prefixes`` to extend the lists, and
    ``allow_servers`` to whitelist trusted servers entirely.

    Why token-based: a regex ``\\bdelete\\b`` doesn't match ``delete_repo``
    because ``_`` is a word character. Splitting on the separator
    side-steps the issue and also keeps ``deletion_service__list``
    (which has 'deletion' in the *server* name, not the tool) safe.
    """

    _DEFAULT_VERBS: frozenset[str] = frozenset({
        "delete", "remove", "rm", "drop", "destroy", "truncate",
        "purge", "kill", "terminate", "wipe", "shutdown",
    })
    _DEFAULT_PREFIXES: tuple[str, ...] = ("force",)

    def __init__(
        self,
        *,
        extra_verbs: tuple[str, ...] = (),
        extra_prefixes: tuple[str, ...] = (),
        allow_servers: tuple[str, ...] = (),
    ) -> None:
        self._verbs: frozenset[str] = self._DEFAULT_VERBS | frozenset(
            v.lower() for v in extra_verbs
        )
        self._prefixes: tuple[str, ...] = self._DEFAULT_PREFIXES + tuple(
            p.lower() for p in extra_prefixes
        )
        self._allow_servers = frozenset(allow_servers)

    async def before_tool_call(
        self, call: ToolCallContent, turn: int,
    ) -> HookDecision | None:
        # Only act on namespaced (MCP) tool calls
        if "__" not in call.name:
            return None
        server, _, tool_part = call.name.partition("__")
        if server in self._allow_servers:
            return None

        # Tokenise the tool half on common separators
        tokens = re.split(r"[_\-]", tool_part.lower())
        for tok in tokens:
            if not tok:
                continue
            if tok in self._verbs:
                return HookDecision(
                    block=True,
                    reason=(
                        f"MCP tool {call.name!r} contains destructive verb "
                        f"{tok!r}; blocked by MCPGuardHook"
                    ),
                )
            for pref in self._prefixes:
                if tok.startswith(pref) and tok != pref[:-1]:
                    return HookDecision(
                        block=True,
                        reason=(
                            f"MCP tool {call.name!r} starts with destructive "
                            f"prefix {pref!r}; blocked by MCPGuardHook"
                        ),
                    )
        return None


# ── Combined guard ────────────────────────────────────────────────────────────

class _CompositeHook(NoopHooks):
    """Chain multiple Hooks together with strict-contract semantics.

    before_llm_call: returns None iff NO hook mutated the messages list. This
    matters because the upstream `_hook_before_llm` helper distinguishes
    "no transformation" from "transformed to (coincidentally) the same value"
    — preserving the None signal lets the loop know it can use the original
    `messages` reference unchanged.

    before_tool_call: runs every hook even after a deny is observed, so audit
    / logging hooks always see every call. The first deny wins for the
    returned decision; subsequent denies' reasons are dropped (callers can
    install their own audit hook to capture them).

    after_tool_call: each hook gets the *current* (possibly-rewritten) result
    so redaction chains compose. Returns None iff no hook rewrote the result.
    """

    def __init__(self, *hooks) -> None:
        self._hooks = hooks

    async def before_llm_call(self, messages, turn):
        current = messages
        changed = False
        for h in self._hooks:
            r = await h.before_llm_call(current, turn)
            if r is not None:
                current = r
                changed = True
        return current if changed else None

    async def before_tool_call(self, call, turn):
        decision: HookDecision | None = None
        for h in self._hooks:
            d = await h.before_tool_call(call, turn)
            # Run ALL hooks (audit must see every call), but the first
            # blocking decision is the one returned.
            if d is not None and d.block and decision is None:
                decision = d
        return decision

    async def after_tool_call(self, call, result, turn):
        current = result
        changed = False
        for h in self._hooks:
            r = await h.after_tool_call(call, current, turn)
            if r is not None:
                current = r
                changed = True
        return current if changed else None
