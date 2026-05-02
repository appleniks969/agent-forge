"""
prompts.py — public prompt-section composables + REPL/autonomous prompt builders.

Depends on session (load_memory_deduped) and system_prompt (SystemPrompt /
SectionName). Exposes the building blocks that compose into a SystemPrompt:
identity / guidelines text constants for every mode, and section helpers for
TOOLS, SKILLS, AGENTS_DOC, REPO_MAP, MEMORY, ENVIRONMENT.

Both composition roots — chat.py and autonomous.py — consume only public
symbols from this module. No private-import leak across composition roots.

Pure file I/O + domain logic. No UI, no ANSI, no event handling.

Owns: TOOLS_SECTION (constant text, group-0 cache), tools_section() helper
      that augments it with plugin-contributed tools; CHAT_IDENTITY /
      CHAT_GUIDELINES; PLAN_IDENTITY / PLAN_GUIDELINES; EXECUTE_IDENTITY /
      EXECUTE_GUIDELINES; VERIFY_IDENTITY / VERIFY_GUIDELINES; environment_section,
      load_agents_doc (AGENTS.md → CLAUDE.md fallback, 32 KB cap), build_repo_map,
      discover_skills; build_chat_prompt() (REPL) and build_autonomous_prompt()
      (PLAN / EXECUTE / VERIFY).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

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
    """Build the tools section, appending one line per non-built-in (plugin) tool.

    Built-in tools use the carefully-worded descriptions in TOOLS_SECTION;
    plugins are described by their Tool.description attributes.
    """
    base = TOOLS_SECTION
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


# ── Autonomous: PLAN phase ────────────────────────────────────────────────────

PLAN_IDENTITY = (
    "You are agent-forge in planning mode inside an isolated git worktree.\n"
    "Your ONLY job right now is to analyse the codebase and produce a precise implementation plan.\n"
    "DO NOT create, edit, or delete any files. Use Read, Grep, and Find to inspect the codebase."
)

PLAN_GUIDELINES = (
    "Output a numbered list of concrete steps. Each step must name the exact file, function, "
    "and change needed so that an engineer could execute it without ambiguity.\n"
    "End your response with the single line: PLAN COMPLETE"
)


# ── Autonomous: EXECUTE phase ─────────────────────────────────────────────────

EXECUTE_IDENTITY = (
    "You are agent-forge running in autonomous mode inside an isolated git worktree.\n"
    "Your job is to complete the assigned task fully and correctly, then stop.\n"
    "There is no human in the loop between turns. Do not ask for confirmation.\n"
    "Do not commit, push, or open a pull request — the delivery system handles that after you finish."
)

EXECUTE_GUIDELINES = (
    "Guidelines:\n"
    "- Read a file before editing it. Use Edit (not Write) for existing files.\n"
    "- After making changes, run the available tests (pytest, npm test, cargo test, go test, etc.)"
    " and fix every failure before considering the task complete. If no tests exist, exercise the"
    " changed code path with a small script you delete afterwards.\n"
    "- Use the minimum number of tool calls. Use Grep and Find before Bash for search.\n"
    "- Use assert statements (not print) in any test or example you write.\n"
    "- Never add TODO comments, placeholder values, or stub implementations."
    " The task is to finish the work, not mark where it would go.\n"
    "- All tool paths are relative to the working directory.\n"
    "- When a tool returns is_error=true, diagnose before retrying. When output is truncated,"
    " refine the call with offset / limit / glob — do not re-run the same call.\n"
    "- If you hit an unrecoverable blocker (missing dependency, ambiguous spec, contradiction with"
    " existing code), describe the blocker clearly and stop. Do not invent a workaround.\n"
    "- End your final reply with a single section titled \"Changes made:\" listing every file you"
    " modified and a one-line reason for each. This becomes the PR/commit body."
)


# ── Autonomous: VERIFY phase ──────────────────────────────────────────────────

VERIFY_IDENTITY = (
    "You are agent-forge in verification mode inside an isolated git worktree.\n"
    "Your job is to verify the implementation by running every available test and "
    "confirming the result matches the task requirements."
)

VERIFY_GUIDELINES = (
    "1. Discover and run all test suites (pytest, npm test, cargo test, go test, etc.).\n"
    "2. If any tests fail, fix them before finishing.\n"
    "3. Confirm the implementation satisfies the original task requirements.\n"
    "4. End your final message with EXACTLY one of:\n"
    "     VERIFICATION PASSED\n"
    "     VERIFICATION FAILED: <one-line reason>"
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
    """Compose the REPL system prompt from public sections."""
    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: CHAT_IDENTITY)
    sp.register(SectionName.TOOLS, lambda: tools_section(tool_registry))
    sp.register(SectionName.GUIDELINES, lambda: CHAT_GUIDELINES)

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


# Back-compat alias — chat.py and external callers still use build_system_prompt.
build_system_prompt = build_chat_prompt


_AUTONOMOUS_TEXTS: dict[str, tuple[str, str]] = {
    "plan":    (PLAN_IDENTITY,    PLAN_GUIDELINES),
    "execute": (EXECUTE_IDENTITY, EXECUTE_GUIDELINES),
    "verify":  (VERIFY_IDENTITY,  VERIFY_GUIDELINES),
}

AutonomousPhase = Literal["plan", "execute", "verify"]


def build_autonomous_prompt(
    phase: AutonomousPhase,
    *,
    cwd: str,
    tool_registry: Any,
    branch: str | None = None,
    worktree_path: str | None = None,
    skills_cwd: str | None = None,
) -> SystemPrompt:
    """Compose a SystemPrompt for one of the three autonomous phases.

    skills_cwd defaults to cwd; autonomous mode passes the repo path here so
    .agent-forge/skills/ in the host repo is discovered even though the worktree
    is a sibling directory without that subtree.
    """
    if phase not in _AUTONOMOUS_TEXTS:
        raise ValueError(f"Unknown phase: {phase!r}. Expected one of {list(_AUTONOMOUS_TEXTS)}")
    identity_text, guidelines_text = _AUTONOMOUS_TEXTS[phase]

    sp = SystemPrompt()
    sp.register(SectionName.IDENTITY, lambda: identity_text)
    sp.register(SectionName.TOOLS, lambda: tools_section(tool_registry))
    sp.register(SectionName.GUIDELINES, lambda: guidelines_text)

    skills_root = skills_cwd if skills_cwd is not None else cwd
    skills_summary = discover_skills(skills_root)
    sp.register(SectionName.SKILLS, lambda: skills_summary)

    # Autonomous environment includes the worktree path + branch + date,
    # but no path-note (the worktree's repo_map isn't built for autonomous).
    sp.register(
        SectionName.ENVIRONMENT,
        lambda: environment_section(
            cwd, branch=branch, worktree_path=worktree_path, include_path_note=False,
        ),
    )
    return sp
