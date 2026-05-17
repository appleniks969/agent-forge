"""Tests for MCP prompt/section/guard integration.

Covers:
  • SectionName.MCP_TOOLS enum: ordering, cache_group, is_volatile
  • SystemPrompt build & invalidate_session behaviour for the new section
  • mcp_tools_section() grouping by server, empty handling
  • tools_section() excludes MCP tools when registry exposes mcp_names()
  • MCPGuardHook: pattern matching, allow_servers, extra_patterns, no-effect
    on non-MCP tools
"""
from __future__ import annotations

import pytest

from agent_forge.guards import (
    BashGuardHook, MCPGuardHook, PathGuardHook, _CompositeHook,
)
from agent_forge.messages import ToolCallContent, ToolDefinition, ToolResult
from agent_forge.prompts import mcp_tools_section, tools_section
from agent_forge.system_prompt import SectionName, SystemPrompt
from agent_forge.tools import ToolRegistry, default_registry


# ── SectionName.MCP_TOOLS ────────────────────────────────────────────────────

def test_mcp_tools_in_section_name_enum():
    assert SectionName.MCP_TOOLS.value == "mcp_tools"


def test_mcp_tools_order_between_guidelines_and_agents_doc():
    assert SectionName.GUIDELINES.order < SectionName.MCP_TOOLS.order < SectionName.AGENTS_DOC.order


def test_mcp_tools_cache_group_is_one():
    """Session-stable, alongside AGENTS_DOC/SKILLS/MEMORY."""
    assert SectionName.MCP_TOOLS.cache_group == 1


def test_mcp_tools_is_not_volatile():
    assert SectionName.MCP_TOOLS.is_volatile is False


def test_all_orders_are_unique_after_insert():
    """The new section must not collide with an existing order slot."""
    orders = [s.order for s in SectionName]
    assert len(orders) == len(set(orders))


def test_all_orders_are_dense():
    """Order values are dense 0…N-1 — no gaps."""
    orders = sorted(s.order for s in SectionName)
    assert orders == list(range(len(orders)))


# ── SystemPrompt integration ─────────────────────────────────────────────────

def test_systemprompt_omits_mcp_tools_when_compute_returns_none():
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: "I am test")
    sp.register(SectionName.MCP_TOOLS, lambda: None)
    sp.register(SectionName.GUIDELINES, lambda: "Be nice")
    sections = sp.build()
    texts = [s.text for s in sections]
    assert "I am test" in texts
    assert "Be nice" in texts
    assert not any("mcp" in t.lower() for t in texts)


def test_systemprompt_renders_mcp_tools_between_guidelines_and_agents_doc():
    """Rendering order: guidelines → mcp_tools → agents_doc."""
    sp = SystemPrompt()
    sp.register(SectionName.GUIDELINES, lambda: "GUIDE")
    sp.register(SectionName.MCP_TOOLS, lambda: "MCP")
    sp.register(SectionName.AGENTS_DOC, lambda: "AGENTS")
    texts = [s.text for s in sp.build()]
    assert texts == ["GUIDE", "MCP", "AGENTS"]


def test_systemprompt_cache_breakpoint_stays_on_memory_in_group_1():
    """MCP_TOOLS sits BEFORE AGENTS_DOC/SKILLS/MEMORY in group 1, so the
    cache_control hint must remain on the last group-1 section (MEMORY),
    not on MCP_TOOLS."""
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: "I")
    sp.register(SectionName.TOOLS, lambda: "T")
    sp.register(SectionName.MCP_TOOLS, lambda: "MCP")
    sp.register(SectionName.AGENTS_DOC, lambda: "A")
    sp.register(SectionName.MEMORY, lambda: "M")
    sections = sp.build()
    # Find the section whose text is "MCP" and confirm cache_control is False
    mcp_sec = next(s for s in sections if s.text == "MCP")
    memory_sec = next(s for s in sections if s.text == "M")
    assert mcp_sec.cache_control is False
    assert memory_sec.cache_control is True


def test_systemprompt_mcp_tools_cached_when_it_is_the_only_group_1_section():
    """If MCP_TOOLS is the *only* group-1 section, IT gets cache_control."""
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: "I")
    sp.register(SectionName.MCP_TOOLS, lambda: "MCP")
    sections = sp.build()
    mcp_sec = next(s for s in sections if s.text == "MCP")
    assert mcp_sec.cache_control is True


def test_invalidate_session_resets_mcp_tools():
    counter = {"n": 0}

    def compute():
        counter["n"] += 1
        return f"call {counter['n']}"

    sp = SystemPrompt()
    sp.register(SectionName.MCP_TOOLS, compute)

    # First build: counter increments to 1
    assert sp.build()[0].text == "call 1"
    # Second build: cached, still 1
    assert sp.build()[0].text == "call 1"
    # Invalidate session → next build re-resolves
    sp.invalidate_session()
    assert sp.build()[0].text == "call 2"


# ── mcp_tools_section() helper ───────────────────────────────────────────────

class _StubTool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.parameters = {"type": "object", "properties": {}}

    def definition(self):
        return ToolDefinition(self.name, self.description, self.parameters)

    async def execute(self, args, *, cwd, signal=None):  # pragma: no cover
        return ToolResult(content="")


def test_mcp_tools_section_returns_none_for_no_mcp():
    reg = default_registry()
    assert mcp_tools_section(reg) is None


def test_mcp_tools_section_groups_by_server():
    reg = ToolRegistry()
    reg.replace_mcp_tools([
        _StubTool("fs__read", "[fs] Read a file"),
        _StubTool("fs__write", "[fs] Write a file"),
        _StubTool("gh__list_repos", "[gh] List repos"),
    ])
    text = mcp_tools_section(reg)
    assert text is not None
    # Servers appear as headings, alphabetically sorted
    assert "Server: fs" in text
    assert "Server: gh" in text
    fs_pos = text.index("Server: fs")
    gh_pos = text.index("Server: gh")
    assert fs_pos < gh_pos
    # Tools listed under their server, sorted
    assert "- fs__read:" in text
    assert "- fs__write:" in text
    assert "- gh__list_repos:" in text


def test_mcp_tools_section_strips_redundant_server_prefix():
    """MCPTool prepends '[server] ' to descriptions; the section heading
    already groups by server, so this prefix is redundant."""
    reg = ToolRegistry()
    reg.replace_mcp_tools([_StubTool("fs__read", "[fs] Read a file")])
    text = mcp_tools_section(reg)
    assert "fs__read: Read a file" in text
    # The bracketed prefix should NOT appear on the description line
    assert "[fs] Read a file" not in text


def test_mcp_tools_section_skips_unnamespaced_entries():
    """Defensive: only namespaced (`__`) names are accepted, even if the
    registry's mcp_names() somehow includes one without a separator."""
    reg = ToolRegistry()
    # Force-tag via replace_mcp_tools, but with a malformed name
    reg.replace_mcp_tools([_StubTool("bogus_no_separator", "x")])
    # The function should ignore the malformed entry and return None
    text = mcp_tools_section(reg)
    assert text is None


def test_mcp_tools_section_falls_back_to_name_convention():
    """Registries without ``mcp_names`` should still detect MCP by ``__``."""
    class _OldRegistry:
        # Simulate a pre-Phase-G registry: only .names() and .get(), no mcp_names()
        def __init__(self, tools):
            self._t = {t.name: t for t in tools}
        def names(self):
            return list(self._t)
        def get(self, n):
            return self._t.get(n)

    reg = _OldRegistry([_StubTool("fs__read", "[fs] R")])
    text = mcp_tools_section(reg)
    assert text is not None and "fs__read" in text


# ── tools_section() excludes MCP tools ───────────────────────────────────────

def test_tools_section_excludes_mcp_tools_from_plugin_list():
    """MCP tools must NOT appear under 'Plugin tools:' — they live in
    their own mcp_tools_section."""
    reg = default_registry()
    reg.replace_mcp_tools([_StubTool("fs__read", "[fs] r")])
    text = tools_section(reg)
    assert "fs__read" not in text
    # Built-in section unchanged
    assert "Bash" in text


def test_tools_section_still_includes_real_plugins():
    """Genuine plugins (non-MCP) must still appear."""
    reg = default_registry()
    plugin = _StubTool("MyPlugin", "Does plugin things")
    reg.register(plugin)
    reg.replace_mcp_tools([_StubTool("fs__x", "[fs]")])
    text = tools_section(reg)
    assert "MyPlugin" in text
    assert "fs__x" not in text


# ── MCPGuardHook ─────────────────────────────────────────────────────────────

def _call(name: str, args: dict | None = None) -> ToolCallContent:
    return ToolCallContent(id="t1", name=name, arguments=args or {})


async def test_mcp_guard_blocks_delete_tools():
    hook = MCPGuardHook()
    decision = await hook.before_tool_call(_call("gh__delete_repo"), turn=1)
    assert decision is not None and decision.block
    assert "delete" in decision.reason.lower()


async def test_mcp_guard_blocks_destructive_verbs():
    hook = MCPGuardHook()
    for name in (
        "db__drop_table",
        "fs__remove_file",
        "service__destroy",
        "log__truncate",
        "queue__purge",
        "git__force_push",
        "process__kill",
        "node__terminate",
    ):
        decision = await hook.before_tool_call(_call(name), turn=1)
        assert decision is not None and decision.block, f"{name} should be blocked"


async def test_mcp_guard_allows_safe_mcp_calls():
    hook = MCPGuardHook()
    for name in (
        "fs__read_file", "gh__list_repos", "db__select", "log__tail",
    ):
        decision = await hook.before_tool_call(_call(name), turn=1)
        assert decision is None, f"{name} should be allowed"


async def test_mcp_guard_ignores_non_mcp_tools():
    """Bash / built-in tools must pass through unchanged."""
    hook = MCPGuardHook()
    for name in ("Bash", "Read", "Write", "Edit", "Grep", "Find", "MyPlugin"):
        decision = await hook.before_tool_call(_call(name), turn=1)
        assert decision is None


async def test_mcp_guard_only_matches_tool_part_not_server():
    """A server name like 'deletion_service' must NOT cause every tool from
    that server to be blocked."""
    hook = MCPGuardHook()
    # tool_part = "list" — safe, even though "deletion" appears in the server name
    decision = await hook.before_tool_call(_call("deletion_service__list"), turn=1)
    assert decision is None


async def test_mcp_guard_allow_servers_whitelists_entire_server():
    hook = MCPGuardHook(allow_servers=("trusted",))
    # Normally blocked
    decision = await hook.before_tool_call(_call("trusted__delete_thing"), turn=1)
    assert decision is None
    # Other servers still blocked
    decision = await hook.before_tool_call(_call("untrusted__delete_thing"), turn=1)
    assert decision is not None and decision.block


async def test_mcp_guard_extra_verbs_extend_defaults():
    """Custom verbs add to (don't replace) the default list."""
    hook = MCPGuardHook(extra_verbs=("yeet",))
    # Default verb still works
    d1 = await hook.before_tool_call(_call("svc__delete_user"), turn=1)
    assert d1 is not None and d1.block
    # New custom verb works
    d2 = await hook.before_tool_call(_call("svc__yeet_thing"), turn=1)
    assert d2 is not None and d2.block


async def test_mcp_guard_composes_with_existing_hooks():
    """The canonical _CompositeHook(BashGuardHook, PathGuardHook,
    MCPGuardHook) must dispatch each call to the right hook."""
    composite = _CompositeHook(BashGuardHook(), PathGuardHook(), MCPGuardHook())
    # Bash command blocked by BashGuard
    d1 = await composite.before_tool_call(
        _call("Bash", {"command": "sudo rm -rf /"}), turn=1,
    )
    assert d1 is not None and d1.block
    # MCP destructive call blocked by MCPGuard
    d2 = await composite.before_tool_call(
        _call("gh__delete_repo"), turn=1,
    )
    assert d2 is not None and d2.block
    # Safe call passes through all three
    d3 = await composite.before_tool_call(
        _call("fs__read_file"), turn=1,
    )
    assert d3 is None
