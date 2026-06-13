"""Skill discovery + the Skill tool: load on-demand instruction bodies by name.

Layer: adapters — imports ports + kernel only (NEVER policy). Skills live in
the same ``.claude/skills/`` layout the wider ecosystem uses: a skill is either
``<root>/<name>/SKILL.md`` or ``<root>/<name>.md``. discover_skills parses ONLY
the frontmatter (name/description) so it is cheap enough to call on every prompt
build; resolve_skill reads a body lazily, on invocation.

Security rationale: a skill body is an untrusted prompt-injection surface (it can
contain instructions authored by whoever dropped the file). It is therefore
returned as a tool RESULT — it enters the conversation as a ToolFinished event,
never the system prompt — so every skill load is auditable in the event log and
the model treats the body as data delivered by a tool, not as standing identity.
This module deliberately does NOT splice skill bodies into any prompt section.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.kernel.types import Effects, ToolResult, ToolSpec
from forge.ports.tool import ToolCtx

_DESC_CAP = 200  # frontmatter description / derived prose line cap (chars)
_BODY_CAP = 64 * 1024  # resolve_skill body cap before a truncation note
_TRUNCATED = "\n\n[Truncated — skill body exceeds 64KB]"


@dataclass(frozen=True)
class SkillMeta:
    """Frontmatter-only view of one skill — cheap to produce, no body read."""

    name: str  # invocation name, e.g. "deep-research" (no leading slash)
    description: str  # trigger hint, <=200 chars
    path: Path  # absolute path to the SKILL.md / <name>.md


# --- frontmatter parsing -----------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter-dict, body). A leading ``---`` line opens a YAML-ish
    block terminated by the next ``---`` line; key:value pairs are hand-parsed
    (no yaml dependency). Absent or unterminated frontmatter yields ({}, text)."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict[str, str] = {}
    idx = 1
    while idx < len(lines):
        if lines[idx].strip() == "---":
            return fm, "\n".join(lines[idx + 1 :])
        key, sep, value = lines[idx].partition(":")
        idx += 1
        if not sep:
            continue
        marker = value.strip()
        if marker and marker[0] in "|>":
            # YAML block scalar: fold the following more-indented lines.
            # '>' joins with spaces (folded), '|' keeps newlines (literal).
            block: list[str] = []
            while idx < len(lines) and (
                not lines[idx].strip() or lines[idx][:1].isspace()
            ):
                if lines[idx].strip() == "---":
                    break
                block.append(lines[idx].strip())
                idx += 1
            joiner = "\n" if marker[0] == "|" else " "
            fm[key.strip().lower()] = joiner.join(block).strip()
        else:
            fm[key.strip().lower()] = marker.strip("'\"")
    # Unterminated block: treat the whole file as body so we never lose content.
    return {}, text


def _first_prose_line(body: str) -> str:
    """First non-blank, non-heading line of the body, capped at _DESC_CAP."""

    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_DESC_CAP]
    return ""


def _fallback_name(path: Path) -> str:
    """Directory name for ``<dir>/SKILL.md``; file stem for ``<name>.md``."""

    if path.name == "SKILL.md":
        return path.parent.name
    return path.stem


def _meta_from_file(path: Path) -> SkillMeta | None:
    """Parse one skill file into a SkillMeta. Returns None on any failure —
    an unreadable or garbled skill is skipped, never raised."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    name = (fm.get("name") or "").strip() or _fallback_name(path)
    if not name:
        return None
    description = (fm.get("description") or "").strip()[:_DESC_CAP]
    if not description:
        description = _first_prose_line(body)
    return SkillMeta(name=name, description=description, path=path.resolve())


def _scan_root(root: Path) -> list[SkillMeta]:
    """All skills directly under one root, both ``<name>/SKILL.md`` and
    ``<name>.md`` layouts. Sorted by name; never raises."""

    metas: list[SkillMeta] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return metas
    for entry in entries:
        try:
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if skill_md.is_file():
                    meta = _meta_from_file(skill_md)
                    if meta is not None:
                        metas.append(meta)
            elif entry.is_file() and entry.suffix == ".md" and entry.name != "SKILL.md":
                meta = _meta_from_file(entry)
                if meta is not None:
                    metas.append(meta)
        except OSError:
            continue
    return metas


# --- per-root cache, keyed on directory mtime --------------------------------

# Calling discover_skills on every prompt build must be cheap; we cache each
# root's scan and re-scan only when the directory's own mtime changes (added /
# removed entries bump it). Keyed by absolute root path; value is (mtime, metas).
_scan_cache: dict[Path, tuple[float, tuple[SkillMeta, ...]]] = {}


def _cached_scan(root: Path) -> tuple[SkillMeta, ...]:
    abs_root = root.resolve()
    try:
        mtime = abs_root.stat().st_mtime
    except OSError:
        _scan_cache.pop(abs_root, None)
        return ()
    cached = _scan_cache.get(abs_root)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    metas = tuple(_scan_root(abs_root))
    _scan_cache[abs_root] = (mtime, metas)
    return metas


def discover_skills(roots: Sequence[Path]) -> tuple[SkillMeta, ...]:
    """Frontmatter-only catalog across roots. De-duped by name (earlier root
    wins — project overrides global), sorted by name. Per-root scan is cached
    on directory mtime so repeated calls within a session are cheap. Never
    raises — an unreadable root or garbled skill is skipped."""

    seen: dict[str, SkillMeta] = {}
    for root in roots:
        for meta in _cached_scan(Path(root)):
            seen.setdefault(meta.name, meta)  # first (earliest root) wins
    return tuple(sorted(seen.values(), key=lambda m: m.name))


def resolve_skill(roots: Sequence[Path], name: str) -> str | None:
    """Full body text (frontmatter stripped) of the named skill, or None if
    absent. Capped at 64KB with a truncation note. Earliest root wins."""

    wanted = (name or "").lstrip("/").strip()
    if not wanted:
        return None
    for meta in discover_skills(roots):
        if meta.name == wanted:
            try:
                text = meta.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            _, body = _split_frontmatter(text)
            body = body.strip()
            if len(body) > _BODY_CAP:
                body = body[:_BODY_CAP] + _TRUNCATED
            return body
    return None


# --- catalog rendering (shared by SkillTool.list and the front index) --------


def render_catalog(metas: Sequence[SkillMeta]) -> str:
    """'<name> — <desc>' lines under a heading; the SkillTool 'list' action and
    front's skills_index_supplier render identically."""

    if not metas:
        return "No skills available."
    lines = ["Available skills:"]
    for meta in metas:
        lines.append(f"{meta.name} — {meta.description}" if meta.description else meta.name)
    return "\n".join(lines)


# --- the Skill tool ----------------------------------------------------------


def _err(msg: str) -> ToolResult:
    return ToolResult(call_id="", content=f"Error: {msg}", is_error=True)


def _ok(content: str) -> ToolResult:
    return ToolResult(call_id="", content=content)


class SkillTool:
    """Load a skill body (action 'get') or the catalog (action 'list').

    effects=READ_PATH: parallel-safe, no Ask. Roots are injected via __init__ so
    the tool stays a pure function of its inputs. The returned body is untrusted
    content — see the module docstring; it is surfaced as a tool result, not a
    prompt section. Never raises; call_id="" (the executor stamps the real id)."""

    spec = ToolSpec(
        name="Skill",
        description=(
            "Load a skill's instructions by name. action 'get' (default) "
            "returns the named skill's body; action 'list' returns the catalog."
        ),
        params={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name to load (required for action 'get')",
                },
                "action": {
                    "type": "string",
                    "enum": ["get", "list"],
                    "default": "get",
                    "description": "'get' loads a skill body; 'list' lists all skills",
                },
            },
        },
        effects=Effects.READ_PATH,
    )

    def __init__(self, roots: Sequence[Path]) -> None:
        self._roots = tuple(Path(r) for r in roots)

    async def run(self, args: Mapping[str, Any], ctx: ToolCtx) -> ToolResult:
        try:
            return self._execute(args)
        except Exception as exc:  # noqa: BLE001 — tools never raise
            return _err(f"{type(exc).__name__}: {exc}")

    def _execute(self, args: Mapping[str, Any]) -> ToolResult:
        action = str(args.get("action") or "get").strip().lower()
        if action == "list":
            return _ok(render_catalog(discover_skills(self._roots)))
        if action != "get":
            return _err(f"unknown action {action!r} — use 'get' or 'list'")
        name = str(args.get("name") or "").strip()
        if not name:
            return _err("action 'get' requires a 'name'. " + self._available_hint())
        body = resolve_skill(self._roots, name)
        if body is None:
            return _err(f"no skill named {name!r}. " + self._available_hint())
        return _ok(body)

    def _available_hint(self) -> str:
        names = [m.name for m in discover_skills(self._roots)]
        if not names:
            return "No skills are available."
        return "Available: " + ", ".join(names)
