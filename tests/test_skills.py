"""Skill discovery + the Skill tool: frontmatter parsing, dedup, resolve, cache.

All fixtures use tmp_path — never the real repo or ~/.claude. The discover_skills
mtime cache is process-global, so tmp_path's unique dirs keep tests isolated.
"""

from __future__ import annotations

import asyncio

import pytest

from forge.adapters.skills import (
    SkillMeta,
    SkillTool,
    discover_skills,
    render_catalog,
    resolve_skill,
)
from forge.adapters.tools.workspace import RootedWorkspace
from forge.ports.tool import ToolCtx
from forge.testing.conformance import check_tool_contract


@pytest.fixture
def ctx(tmp_path):
    return ToolCtx(ws=RootedWorkspace(tmp_path), cancel=asyncio.Event())


def _write_dir_skill(root, name, *, fm=True, description="does a thing", body="Do the thing."):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n---\n" if fm else ""
    (d / "SKILL.md").write_text(front + body, encoding="utf-8")
    return d / "SKILL.md"


def _write_flat_skill(root, name, *, fm=True, description="flat thing", body="Flat body."):
    root.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n---\n" if fm else ""
    p = root / f"{name}.md"
    p.write_text(front + body, encoding="utf-8")
    return p


# --- frontmatter parsing + fallbacks -----------------------------------------


def test_frontmatter_name_and_description(tmp_path):
    _write_dir_skill(tmp_path, "deep-research", description="fan out web searches")
    (skill,) = discover_skills([tmp_path])
    assert skill.name == "deep-research"
    assert skill.description == "fan out web searches"
    assert skill.path.is_absolute()


def test_folded_block_scalar_description(tmp_path):
    # Real skills (agent-forge-eval/wiki) use YAML '>' folded block scalars:
    # the value is '>' then indented continuation lines, joined with spaces.
    d = tmp_path / "eval"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: eval\ndescription: >\n  Run structured benchmarks\n"
        "  across coding tasks.\n---\nbody",
        encoding="utf-8",
    )
    (skill,) = discover_skills([tmp_path])
    assert skill.description == "Run structured benchmarks across coding tasks."


def test_literal_block_scalar_keeps_newlines(tmp_path):
    d = tmp_path / "lit"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: lit\ndescription: |\n  line one\n  line two\n---\nbody",
        encoding="utf-8",
    )
    (skill,) = discover_skills([tmp_path])
    assert skill.description == "line one\nline two"


def test_name_falls_back_to_dir_for_skill_md(tmp_path):
    # No 'name' key -> directory name wins for <dir>/SKILL.md.
    d = tmp_path / "my-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: x\n---\nbody", encoding="utf-8")
    (skill,) = discover_skills([tmp_path])
    assert skill.name == "my-skill"


def test_name_falls_back_to_stem_for_flat_md(tmp_path):
    p = tmp_path / "loner.md"
    p.write_text("---\ndescription: x\n---\nbody", encoding="utf-8")
    (skill,) = discover_skills([tmp_path])
    assert skill.name == "loner"


def test_description_falls_back_to_first_prose_line(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    # No description in frontmatter; first non-heading prose line is used.
    (d / "SKILL.md").write_text(
        "---\nname: s\n---\n# A Heading\n\nThe real first line.\n",
        encoding="utf-8",
    )
    (skill,) = discover_skills([tmp_path])
    assert skill.description == "The real first line."


def test_no_frontmatter_uses_dirname_and_first_line(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# Title\nFirst prose.\n", encoding="utf-8")
    (skill,) = discover_skills([tmp_path])
    assert skill.name == "bare"
    assert skill.description == "First prose."


def test_description_capped_at_200(tmp_path):
    _write_dir_skill(tmp_path, "long", description="x" * 500)
    (skill,) = discover_skills([tmp_path])
    assert len(skill.description) == 200


def test_quoted_frontmatter_values_unquoted(tmp_path):
    d = tmp_path / "q"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: \"q\"\ndescription: 'quoted desc'\n---\nbody",
        encoding="utf-8",
    )
    (skill,) = discover_skills([tmp_path])
    assert skill.name == "q"
    assert skill.description == "quoted desc"


def test_sorted_by_name(tmp_path):
    _write_dir_skill(tmp_path, "zebra")
    _write_dir_skill(tmp_path, "alpha")
    _write_flat_skill(tmp_path, "mango")
    names = [m.name for m in discover_skills([tmp_path])]
    assert names == ["alpha", "mango", "zebra"]


# --- project-overrides-global dedup ------------------------------------------


def test_project_overrides_global_earlier_root_wins(tmp_path):
    project = tmp_path / "project"
    glob = tmp_path / "global"
    _write_dir_skill(project, "shared", description="project version", body="PROJECT")
    _write_dir_skill(glob, "shared", description="global version", body="GLOBAL")
    _write_dir_skill(glob, "global-only", description="only global")
    # project root listed first => it wins the name clash.
    metas = discover_skills([project, glob])
    by_name = {m.name: m for m in metas}
    assert by_name["shared"].description == "project version"
    assert "global-only" in by_name
    # resolve_skill follows the same precedence.
    assert resolve_skill([project, glob], "shared") == "PROJECT"


# --- resolve_skill -----------------------------------------------------------


def test_resolve_strips_frontmatter(tmp_path):
    _write_dir_skill(tmp_path, "s", body="Body line one.\nBody line two.")
    body = resolve_skill([tmp_path], "s")
    assert body == "Body line one.\nBody line two."
    assert "name:" not in body


def test_resolve_accepts_leading_slash(tmp_path):
    _write_dir_skill(tmp_path, "s", body="hello")
    assert resolve_skill([tmp_path], "/s") == "hello"


def test_resolve_missing_returns_none(tmp_path):
    _write_dir_skill(tmp_path, "s")
    assert resolve_skill([tmp_path], "absent") is None


def test_resolve_empty_name_returns_none(tmp_path):
    _write_dir_skill(tmp_path, "s")
    assert resolve_skill([tmp_path], "") is None


def test_resolve_truncation_note(tmp_path):
    big = "y" * (70 * 1024)
    _write_dir_skill(tmp_path, "big", body=big)
    body = resolve_skill([tmp_path], "big")
    assert body is not None
    assert body.endswith("[Truncated — skill body exceeds 64KB]")
    assert len(body) <= 64 * 1024 + 64


# --- garbled / unreadable is skipped, not raised -----------------------------


def test_garbled_skill_skipped_not_raised(tmp_path):
    # Invalid UTF-8 bytes: read with errors="replace" so it does not crash.
    d = tmp_path / "garbled"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"---\nname: garbled\n---\n\xff\xfe bad bytes")
    _write_dir_skill(tmp_path, "good")
    metas = discover_skills([tmp_path])
    names = {m.name for m in metas}
    # Must not raise; the good skill still resolves.
    assert "good" in names


def test_missing_root_is_skipped(tmp_path):
    _write_dir_skill(tmp_path, "good")
    metas = discover_skills([tmp_path / "does-not-exist", tmp_path])
    assert {m.name for m in metas} == {"good"}


def test_empty_dir_skill_skipped_when_no_name(tmp_path):
    # A directory with no SKILL.md and no .md contributes nothing.
    (tmp_path / "emptydir").mkdir()
    assert discover_skills([tmp_path]) == ()


# --- cache freshness ---------------------------------------------------------


def test_cache_refreshes_after_adding_skill(tmp_path):
    _write_dir_skill(tmp_path, "first")
    assert {m.name for m in discover_skills([tmp_path])} == {"first"}
    # Adding a new skill dir bumps the root mtime; next call must see it.
    _write_dir_skill(tmp_path, "second")
    assert {m.name for m in discover_skills([tmp_path])} == {"first", "second"}


# --- SkillTool ---------------------------------------------------------------


async def test_tool_get_returns_body(ctx, tmp_path):
    _write_dir_skill(tmp_path, "s", body="the instructions")
    res = await SkillTool([tmp_path]).run({"name": "s"}, ctx)
    assert not res.is_error
    assert res.content == "the instructions"


async def test_tool_get_is_default_action(ctx, tmp_path):
    _write_dir_skill(tmp_path, "s", body="default get")
    res = await SkillTool([tmp_path]).run({"name": "s", "action": "get"}, ctx)
    assert res.content == "default get"


async def test_tool_list_renders_catalog(ctx, tmp_path):
    _write_dir_skill(tmp_path, "alpha", description="first")
    _write_flat_skill(tmp_path, "beta", description="second")
    res = await SkillTool([tmp_path]).run({"action": "list"}, ctx)
    assert not res.is_error
    assert "alpha — first" in res.content
    assert "beta — second" in res.content


async def test_tool_unknown_name_lists_available(ctx, tmp_path):
    _write_dir_skill(tmp_path, "alpha")
    _write_dir_skill(tmp_path, "beta")
    res = await SkillTool([tmp_path]).run({"name": "nope"}, ctx)
    assert res.is_error
    assert "nope" in res.content
    assert "alpha" in res.content and "beta" in res.content


async def test_tool_get_without_name_is_error_with_hint(ctx, tmp_path):
    _write_dir_skill(tmp_path, "alpha")
    res = await SkillTool([tmp_path]).run({}, ctx)
    assert res.is_error
    assert "requires a 'name'" in res.content
    assert "alpha" in res.content


async def test_tool_unknown_action_is_error(ctx, tmp_path):
    _write_dir_skill(tmp_path, "alpha")
    res = await SkillTool([tmp_path]).run({"action": "delete"}, ctx)
    assert res.is_error
    assert "unknown action" in res.content


async def test_tool_satisfies_contract_and_never_raises(ctx, tmp_path):
    _write_dir_skill(tmp_path, "s")
    tool = SkillTool([tmp_path])
    # Garbage args must not raise; the conformance kit enforces it.
    res = await check_tool_contract(tool, {"name": 123, "action": None}, ctx)
    assert isinstance(res.content, str)


def test_render_catalog_empty():
    assert render_catalog([]) == "No skills available."


def test_skill_meta_is_frozen():
    meta = SkillMeta(name="n", description="d", path=__import__("pathlib").Path("/x"))
    with pytest.raises(Exception):
        meta.name = "other"  # type: ignore[misc]
