"""
prompts.py — SystemPrompt builder: identity, tools, guidelines, repo map, memory.

Depends on context (SystemPrompt, SectionName) and session (load_memory_deduped).
Exists between the lower layers (session/context) and the composition roots
(chat.py, autonomous.py) so that prompt assembly logic — AGENTS.md loading,
repo map construction, memory merging — is not inlined into the REPL or the
state machine.

Pure file I/O + domain logic. No UI, no ANSI, no event handling.
Owns: build_system_prompt() (REPL variant), _load_agents_doc() (AGENTS.md →
      CLAUDE.md fallback, 32 KB cap), _build_repo_map(), _TOOLS_SECTION and
      _CHAT_GUIDELINES constants (group-0 stable cache text).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .session import load_memory_deduped
from .system_prompt import SectionName, SystemPrompt

# Shared by both chat and autonomous prompts (group-0 cache — no volatile content).
_TOOLS_SECTION = (
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

_CHAT_GUIDELINES = (
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


def _build_tools_section(tool_registry: Any) -> str:
    """
    Build the tools section for the system prompt.

    Built-in tools use the carefully-worded descriptions defined in _TOOLS_SECTION.
    Plugin-contributed tools are appended using their own Tool.description attributes.
    """
    base = _TOOLS_SECTION
    try:
        builtin_names = {"Bash", "Read", "Write", "Edit", "Grep", "Find"}
        plugin_names = [
            n for n in (tool_registry.names() if hasattr(tool_registry, "names") else [])
            if n not in builtin_names
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


def build_system_prompt(
    cfg: "ChatConfig",
    tool_registry: Any,
    *,
    plugin_registry: Any = None,
) -> SystemPrompt:  # noqa: F821
    sp = SystemPrompt()

    sp.register(SectionName.IDENTITY, lambda: (
        "You are Claude Code, an interactive CLI tool that helps users with software engineering tasks.\n"
        "You help users by reading files, executing commands, editing code, and writing new files.\n"
        "You are operating in interactive chat mode — a human is reading your replies and may correct you between turns."
    ))
    # Tools section is dynamic so plugin-contributed tools appear automatically
    sp.register(SectionName.TOOLS, lambda: _build_tools_section(tool_registry))
    sp.register(SectionName.GUIDELINES, lambda: _CHAT_GUIDELINES)

    agents_md = _load_agents_doc(cfg.cwd)
    sp.register(SectionName.AGENTS_DOC, lambda: agents_md)

    # Fix 6: wire skills into the system prompt so the LLM knows they exist
    skills_summary = _discover_skills(cfg.cwd)
    sp.register(SectionName.SKILLS, lambda: skills_summary)

    memory = load_memory_deduped(cfg.cwd, [agents_md or ""])
    sp.register(SectionName.MEMORY, lambda: memory if memory.strip() else None)

    repo_map = _build_repo_map(cfg.cwd)
    sp.register(SectionName.REPO_MAP, lambda: repo_map)

    sp.register(SectionName.ENVIRONMENT, lambda: (
        f"Working directory: {cfg.cwd}\n"
        f"Date: {__import__('datetime').date.today()}\n"
        "All file paths are relative to the working directory. Use paths exactly as shown in the repository map."
    ))

    if cfg.custom_system_prompt:
        sp.register(SectionName.CUSTOM, lambda: cfg.custom_system_prompt)

    # Inject sections from PromptPlugins (appended as volatile extras, no caching)
    if plugin_registry is not None:
        plugin_registry.inject_prompt_sections(sp)

    return sp


def _discover_skills(cwd: str) -> str | None:
    """
    Fix 6: discover skill markdown files in <cwd>/.agent-forge/skills/.
    Returns a one-line-per-skill summary, e.g.:
      Available skills:
      /implement — implement a feature end-to-end from spec to tests
      /review — review code for correctness, style, and test coverage
    Returns None when no skill files exist (section omitted from system prompt).
    """
    skills_dir = Path(cwd) / ".agent-forge" / "skills"
    if not skills_dir.is_dir():
        return None
    lines: list[str] = []
    for skill_file in sorted(skills_dir.glob("*.md")):
        try:
            text = skill_file.read_text(encoding="utf-8").strip()
            # Derive the skill name: strip .md, keep leading /
            skill_name = "/" + skill_file.stem.lstrip("/")
            # First non-empty, non-heading line is the description
            description = ""
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:120]
                    break
            if not description:
                # Fall back to the first heading content
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


def _load_agents_doc(cwd: str) -> str | None:
    cap = 32 * 1024
    for name in ("AGENTS.md", "CLAUDE.md", ".agent-forge/instructions.md"):
        p = Path(cwd) / name
        if p.exists():
            text = p.read_text(encoding="utf-8")
            if len(text) > cap:
                text = text[:cap] + "\n\n[Truncated — file exceeds 32KB]"
            return text
    return None


def _build_repo_map(cwd: str) -> str | None:
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
