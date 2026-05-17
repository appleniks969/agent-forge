"""
prompts.py — public prompt-section composables + REPL prompt builder.

Depends on session (load_memory_deduped) and system_prompt (SystemPrompt /
SectionName). Exposes the building blocks that compose into a SystemPrompt:
identity / guidelines text constants, and section helpers for TOOLS, SKILLS,
AGENTS_DOC, REPO_MAP, MEMORY, ENVIRONMENT.

The composition root (chat.py) consumes only public symbols from this module.

Pure file I/O + domain logic. No UI, no ANSI, no event handling.

Owns: TOOLS_SECTION (constant text, group-0 cache), tools_section() helper
      that augments it with plugin-contributed tools; CHAT_IDENTITY /
      CHAT_GUIDELINES; environment_section, load_agents_doc (AGENTS.md →
      CLAUDE.md fallback, 32 KB cap), build_repo_map, discover_skills;
      build_chat_prompt() (REPL).
"""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from .session import load_memory_deduped
from .system_prompt import SectionName, SystemPrompt

# ── Tools section (group-0 cache, stable across all modes) ────────────────────

TOOLS_SECTION = (
    "Available tools:\n"
    "- Bash: Run a shell command in the working directory. stdout+stderr combined; truncated to 2000 lines or 50 KB"
    " (full output saved to a temp file shown in the output). Default timeout 120 s. Use for tests, builds, git,"
    " and shell-only work. Avoid interactive or long-running commands.\n"
    "- Read: Read a file with line numbers. Truncated to 2000 lines or 50 KB; use offset and limit to page through"
    " large files. Prefer over cat, head, tail, sed.\n"
    "- Write: Create a new file or fully overwrite an existing one. Creates parent directories. Use only for new"
    " files or complete rewrites — for incremental changes, use Edit.\n"
    "- Edit: Replace exact text in an existing file. old_string must appear verbatim and uniquely; if it appears"
    " more than once, set replace_all=true. In a multi-edit call each old_string is matched against the original"
    " file, not the running result. Read the file first to get the exact text.\n"
    "- Grep: Search file contents by regex. Returns file:line matches. Scope with the glob parameter. Respects"
    " .gitignore. Truncated to 100 matches or 50 KB. Prefer over Bash grep / rg.\n"
    "- Find: List files by glob pattern, sorted by mtime newest first. Scope with the path parameter. Respects"
    " .gitignore. Truncated to 1000 results or 50 KB. Prefer over Bash find or ls -R."
)


def tools_section(tool_registry: Any) -> str:
    """Build the tools section, appending one line per non-built-in plugin tool.

    Scope:
      - Built-in tools use the carefully-worded descriptions in TOOLS_SECTION.
      - Plugins (non-MCP, custom-registered) get one line from their
        ``Tool.description``.
      - MCP tools are deliberately EXCLUDED — they live in their own
        ``mcp_tools_section()`` (registered as ``SectionName.MCP_TOOLS``,
        cache_group 1) so they invalidate independently on /mcp reconnect.

    Identifying MCP-sourced tools:
      We prefer ``tool_registry.mcp_names()`` (Phase G) when available;
      fall back to the namespacing convention ``{server}__{tool}`` so
      this remains correct for ad-hoc registries without the tag.
    """
    base = TOOLS_SECTION
    try:
        builtin_names = {"Bash", "Read", "Write", "Edit", "Grep", "Find"}
        all_names = list(tool_registry.names() if hasattr(tool_registry, "names") else [])
        mcp_names = set(
            tool_registry.mcp_names()
            if hasattr(tool_registry, "mcp_names")
            else (n for n in all_names if "__" in n)
        )
        plugin_names = [
            n for n in all_names
            if n not in builtin_names and n not in mcp_names
        ]
        if not plugin_names:
            return base
        extra_lines = ["\nPlugin tools:"]
        for tname in plugin_names:
            tool = tool_registry.get(tname)
            desc = (getattr(tool, "description", "") or "").strip()
            extra_lines.append(f"- {tname}: {desc}" if desc else f"- {tname}")
        return base + "\n".join(extra_lines)
    except Exception:
        return base


def mcp_tools_section(tool_registry: Any) -> str | None:
    """Build the MCP tools section, grouped by server.

    Returns ``None`` when no MCP tools are registered — the caller (a
    ``SystemPrompt`` builder) will omit the section entirely so the
    cache group 1 layout for non-MCP sessions is unchanged.

    Grouping: tools are bucketed by the prefix before ``__`` (the server
    name). Within a server they're sorted by name for stable cache hits.
    Server descriptions strip the ``[server] `` prefix that ``MCPTool``
    adds (we already group by server, no need to repeat it on every line).
    """
    try:
        all_names = list(tool_registry.names() if hasattr(tool_registry, "names") else [])
        mcp_names = list(
            tool_registry.mcp_names()
            if hasattr(tool_registry, "mcp_names")
            else (n for n in all_names if "__" in n)
        )
    except Exception:
        return None
    if not mcp_names:
        return None

    by_server: dict[str, list[str]] = {}
    for full in mcp_names:
        if "__" not in full:
            continue
        server, _, _ = full.partition("__")
        by_server.setdefault(server, []).append(full)

    if not by_server:
        return None

    lines: list[str] = [
        "MCP tools (from external Model Context Protocol servers):"
    ]
    for server in sorted(by_server):
        lines.append(f"\nServer: {server}")
        for tname in sorted(by_server[server]):
            tool = tool_registry.get(tname)
            desc = (getattr(tool, "description", "") or "").strip()
            # MCPTool prepends "[server] " — strip it for readability under
            # the per-server heading.
            prefix = f"[{server}] "
            if desc.startswith(prefix):
                desc = desc[len(prefix):]
            lines.append(f"- {tname}: {desc}" if desc else f"- {tname}")
    return "\n".join(lines)


# ── REPL identity / guidelines (chat.py mode) ─────────────────────────────────

CHAT_IDENTITY = (
    "You are Claude Code, an interactive CLI tool that helps users with software engineering tasks.\n"
    "You help users by reading files, executing commands, editing code, and writing new files.\n"
    "You are operating in interactive chat mode — a human is reading your replies and may correct you between turns."
)

CHAT_GUIDELINES = (
    "Guidelines:\n"
    "- Be concise in your responses\n"
    "- Show file paths clearly when working with files\n"
    "- Use Bash for shell operations, tests, builds, and git\n"
    "- Use Read to examine files instead of cat or sed\n"
    "- Use Edit for precise changes (old_string must match exactly and be unique in the file)\n"
    "- When editing multiple separate locations in one file, use one Edit call\n"
    "- old_string is matched against the original file, not after earlier edits are applied\n"
    "- Keep old_string as small as possible while still being unique\n"
    "- Use Write only for new files or complete rewrites\n"
    "- Use Grep to search file contents by pattern, Find to list files by glob\n"
    "\n"
    "Planning gate\n"
    "- If the user is asking — not telling — answer in prose without using tools.\n"
    "- If intent is ambiguous, ask one clarifying question. Do not read files or run commands to disambiguate.\n"
    "\n"
    "Safety\n"
    "- Confirm before destructive operations: rm -rf, git reset --hard, force-push, mass deletes.\n"
    "- Never commit, push, or open a PR unless the user explicitly asks."
)


# ── Section helpers (file I/O + composition) ──────────────────────────────────

def discover_skills(cwd: str) -> str | None:
    """Discover skill markdown files in <cwd>/.agent-forge/skills/.

    Returns one-line-per-skill summary like:
      Available skills:
      /implement — implement a feature end-to-end from spec to tests

    Returns None if the directory doesn't exist or no skills resolved.
    """
    skills_dir = Path(cwd) / ".agent-forge" / "skills"
    if not skills_dir.is_dir():
        return None
    lines: list[str] = []
    for skill_file in sorted(skills_dir.glob("*.md")):
        try:
            text = skill_file.read_text(encoding="utf-8").strip()
            skill_name = "/" + skill_file.stem.lstrip("/")
            description = ""
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:120]
                    break
            if not description:
                for line in text.splitlines():
                    stripped = line.lstrip("# ").strip()
                    if stripped:
                        description = stripped[:120]
                        break
            if description:
                lines.append(f"{skill_name} — {description}")
        except Exception:
            continue
    if not lines:
        return None
    return "Available skills:\n" + "\n".join(lines)


def load_agents_doc(cwd: str) -> str | None:
    """Load AGENTS.md / CLAUDE.md / .agent-forge/instructions.md (32 KB cap)."""
    cap = 32 * 1024
    for name in ("AGENTS.md", "CLAUDE.md", ".agent-forge/instructions.md"):
        p = Path(cwd) / name
        if p.exists():
            text = p.read_text(encoding="utf-8")
            if len(text) > cap:
                text = text[:cap] + "\n\n[Truncated — file exceeds 32KB]"
            return text
    return None


def build_repo_map(cwd: str) -> str | None:
    """Return 'Repository files:\\n<path>\\n…' for up to 200 files."""
    try:
        root = Path(cwd)
        ignore = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
        files: list[str] = []
        for p in sorted(root.rglob("*")):
            if any(part in ignore for part in p.parts):
                continue
            if p.is_file():
                files.append(str(p.relative_to(root)))
            if len(files) >= 500:
                break
        if not files:
            return None
        return "Repository files:\n" + "\n".join(files[:200])
    except Exception:
        return None


def environment_section(
    cwd: str,
    *,
    branch: str | None = None,
    worktree_path: str | None = None,
    include_path_note: bool = True,
) -> str:
    """Build the volatile ENVIRONMENT section (working dir + date + optional branch)."""
    lines = [f"Working directory: {worktree_path or cwd}"]
    if branch:
        lines.append(f"Branch: {branch}")
    lines.append(f"Date: {date.today()}")
    if include_path_note:
        lines.append(
            "All file paths are relative to the working directory."
            " Use paths exactly as shown in the repository map."
        )
    return "\n".join(lines)


# ── Composers ─────────────────────────────────────────────────────────────────

def build_chat_prompt(
    cfg: "ChatConfig",  # noqa: F821
    tool_registry: Any,
    *,
    plugin_registry: Any = None,
) -> SystemPrompt:
    """Compose the REPL system prompt synchronously.

    All file I/O (build_repo_map, load_agents_doc, discover_skills,
    load_memory_deduped) runs at sp.build() time inside registered lambdas.
    Acceptable for one-shot setup, but blocks the event loop if called from
    async code. Prefer build_chat_prompt_async() in event-loop contexts.
    """
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: CHAT_IDENTITY)
    sp.register(SectionName.TOOLS, lambda: tools_section(tool_registry))
    sp.register(SectionName.GUIDELINES, lambda: CHAT_GUIDELINES)
    # MCP_TOOLS (Phase I) — registered eagerly so /mcp reconnect re-resolves
    # via SystemPrompt.invalidate_session(); returns None when no MCP tools
    # are present, keeping the cache-breakpoint layout unchanged for users
    # who don't use MCP.
    sp.register(SectionName.MCP_TOOLS, lambda: mcp_tools_section(tool_registry))

    agents_md = load_agents_doc(cfg.cwd)
    sp.register(SectionName.AGENTS_DOC, lambda: agents_md)

    skills_summary = discover_skills(cfg.cwd)
    sp.register(SectionName.SKILLS, lambda: skills_summary)

    memory = load_memory_deduped(cfg.cwd, [agents_md or ""])
    sp.register(SectionName.MEMORY, lambda: memory if memory.strip() else None)

    repo_map = build_repo_map(cfg.cwd)
    sp.register(SectionName.REPO_MAP, lambda: repo_map)

    sp.register(
        SectionName.ENVIRONMENT,
        lambda: environment_section(cfg.cwd),
    )

    if cfg.custom_system_prompt:
        sp.register(SectionName.CUSTOM, lambda: cfg.custom_system_prompt)

    if plugin_registry is not None:
        plugin_registry.inject_prompt_sections(sp)

    return sp


async def build_chat_prompt_async(
    cfg: "ChatConfig",  # noqa: F821
    tool_registry: Any,
    *,
    plugin_registry: Any = None,
) -> SystemPrompt:
    """Async variant: eagerly resolves I/O-heavy sections in worker threads.

    build_repo_map walks the entire repo via rglob — on large repos this can
    take 100+ ms. Calling it inline on the event loop blocks every other
    pending task. We pre-compute it (and other sync I/O) via asyncio.to_thread
    and pass the result as a captured value to the SystemPrompt lambda.
    """
    agents_md_task = asyncio.to_thread(load_agents_doc, cfg.cwd)
    skills_task = asyncio.to_thread(discover_skills, cfg.cwd)
    repo_map_task = asyncio.to_thread(build_repo_map, cfg.cwd)
    agents_md, skills_summary, repo_map = await asyncio.gather(
        agents_md_task, skills_task, repo_map_task,
    )
    memory = await asyncio.to_thread(load_memory_deduped, cfg.cwd, [agents_md or ""])

    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: CHAT_IDENTITY)
    sp.register(SectionName.TOOLS, lambda: tools_section(tool_registry))
    sp.register(SectionName.GUIDELINES, lambda: CHAT_GUIDELINES)
    # MCP_TOOLS — see build_chat_prompt() for the rationale.
    sp.register(SectionName.MCP_TOOLS, lambda: mcp_tools_section(tool_registry))
    sp.register(SectionName.AGENTS_DOC, lambda: agents_md)
    sp.register(SectionName.SKILLS, lambda: skills_summary)
    sp.register(SectionName.MEMORY, lambda: memory if memory.strip() else None)
    sp.register(SectionName.REPO_MAP, lambda: repo_map)
    sp.register(
        SectionName.ENVIRONMENT,
        lambda: environment_section(cfg.cwd),
    )
    if cfg.custom_system_prompt:
        sp.register(SectionName.CUSTOM, lambda: cfg.custom_system_prompt)
    if plugin_registry is not None:
        plugin_registry.inject_prompt_sections(sp)
    return sp
