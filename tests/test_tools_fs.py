"""ReadTool/WriteTool/EditTool: behavior, containment, and overlap detection."""

from __future__ import annotations

import asyncio

import pytest

from forge.adapters.tools.fs import EditTool, ReadTool, WriteTool
from forge.adapters.tools.workspace import RootedWorkspace
from forge.ports.tool import ToolCtx
from forge.testing.conformance import check_tool_contract


@pytest.fixture
def ctx(tmp_path):
    return ToolCtx(ws=RootedWorkspace(tmp_path), cancel=asyncio.Event())


# --- ReadTool ----------------------------------------------------------------


async def test_read_numbers_lines(ctx, tmp_path):
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n")
    res = await ReadTool().run({"path": "f.txt"}, ctx)
    assert not res.is_error
    assert res.content == "1\talpha\n2\tbeta\n3\tgamma\n"


async def test_read_offset_limit_window(ctx, tmp_path):
    (tmp_path / "f.txt").write_text("".join(f"line{i}\n" for i in range(1, 6)))
    res = await ReadTool().run({"path": "f.txt", "offset": 2, "limit": 2}, ctx)
    assert not res.is_error
    assert res.content.startswith("2\tline2\n3\tline3\n")
    assert "[2 more lines — use offset=4 to continue]" in res.content


async def test_read_missing_file_is_error(ctx):
    res = await ReadTool().run({"path": "nope.txt"}, ctx)
    assert res.is_error
    assert "file not found" in res.content


async def test_read_directory_is_error(ctx, tmp_path):
    (tmp_path / "d").mkdir()
    res = await ReadTool().run({"path": "d"}, ctx)
    assert res.is_error


async def test_read_escape_returns_error_not_raise(ctx):
    res = await check_tool_contract(ReadTool(), {"path": "../secrets.txt"}, ctx)
    assert res.is_error
    assert "escapes" in res.content


async def test_read_absolute_escape_is_error(ctx):
    res = await ReadTool().run({"path": "/etc/passwd"}, ctx)
    assert res.is_error


# --- WriteTool -----------------------------------------------------------------


async def test_write_creates_file_and_parents(ctx, tmp_path):
    res = await WriteTool().run({"path": "a/b/new.txt", "content": "one\ntwo\n"}, ctx)
    assert not res.is_error
    assert (tmp_path / "a" / "b" / "new.txt").read_text() == "one\ntwo\n"


async def test_write_overwrites(ctx, tmp_path):
    (tmp_path / "f.txt").write_text("old")
    res = await WriteTool().run({"path": "f.txt", "content": "new"}, ctx)
    assert not res.is_error
    assert (tmp_path / "f.txt").read_text() == "new"


async def test_write_escape_is_error(ctx, tmp_path):
    res = await WriteTool().run({"path": "../evil.txt", "content": "x"}, ctx)
    assert res.is_error
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_write_missing_content_is_error(ctx):
    res = await WriteTool().run({"path": "f.txt"}, ctx)
    assert res.is_error


async def test_write_when_cancelled_does_not_mutate(ctx, tmp_path):
    ctx.cancel.set()
    res = await WriteTool().run({"path": "f.txt", "content": "x"}, ctx)
    assert res.is_error
    assert not (tmp_path / "f.txt").exists()


# --- EditTool ------------------------------------------------------------------


@pytest.fixture
def edited(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def alpha():\n    return 1\n\ndef beta():\n    return 1\n")
    return f


async def test_edit_unique_match_replaces(ctx, edited):
    res = await EditTool().run(
        {"path": "code.py", "old_string": "def alpha():", "new_string": "def gamma():"}, ctx
    )
    assert not res.is_error
    assert "def gamma():" in edited.read_text()
    assert "def alpha():" not in edited.read_text()


async def test_edit_not_found_is_error(ctx, edited):
    before = edited.read_text()
    res = await EditTool().run(
        {"path": "code.py", "old_string": "def delta():", "new_string": "x"}, ctx
    )
    assert res.is_error
    assert "not found" in res.content
    assert edited.read_text() == before


async def test_edit_ambiguous_match_requires_replace_all(ctx, edited):
    before = edited.read_text()
    res = await EditTool().run(
        {"path": "code.py", "old_string": "    return 1", "new_string": "    return 2"}, ctx
    )
    assert res.is_error
    assert "replace_all" in res.content
    assert edited.read_text() == before


async def test_edit_replace_all(ctx, edited):
    res = await EditTool().run(
        {
            "path": "code.py",
            "old_string": "    return 1",
            "new_string": "    return 2",
            "replace_all": True,
        },
        ctx,
    )
    assert not res.is_error
    assert edited.read_text().count("    return 2") == 2


async def test_edit_empty_old_string_is_error(ctx, edited):
    res = await EditTool().run({"path": "code.py", "old_string": "", "new_string": "x"}, ctx)
    assert res.is_error


async def test_edit_multi_batch_applies_in_order(ctx, edited):
    res = await EditTool().run(
        {
            "path": "code.py",
            "edits": [
                {"old_string": "def alpha():", "new_string": "def first():"},
                {"old_string": "def beta():", "new_string": "def second():"},
            ],
        },
        ctx,
    )
    assert not res.is_error
    text = edited.read_text()
    assert "def first():" in text and "def second():" in text


async def test_edit_batch_validates_against_original(ctx, edited):
    # The second edit references text that only exists after the first —
    # original-based matching must reject it.
    before = edited.read_text()
    res = await EditTool().run(
        {
            "path": "code.py",
            "edits": [
                {"old_string": "def alpha():", "new_string": "def gamma():"},
                {"old_string": "def gamma():", "new_string": "def delta():"},
            ],
        },
        ctx,
    )
    assert res.is_error
    assert edited.read_text() == before


async def test_edit_overlap_identical_old_strings_rejected(ctx, edited):
    before = edited.read_text()
    res = await EditTool().run(
        {
            "path": "code.py",
            "edits": [
                {"old_string": "def alpha():", "new_string": "x"},
                {"old_string": "def alpha():", "new_string": "y"},
            ],
        },
        ctx,
    )
    assert res.is_error
    assert "identical" in res.content
    assert edited.read_text() == before


async def test_edit_overlap_containment_rejected(ctx, edited):
    before = edited.read_text()
    res = await EditTool().run(
        {
            "path": "code.py",
            "edits": [
                {"old_string": "def alpha():", "new_string": "x"},
                {"old_string": "alpha", "new_string": "y"},
            ],
        },
        ctx,
    )
    assert res.is_error
    assert "overlap" in res.content
    assert edited.read_text() == before


async def test_edit_identical_old_strings_both_replace_all_ok(ctx, edited):
    res = await EditTool().run(
        {
            "path": "code.py",
            "edits": [
                {"old_string": "return 1", "new_string": "return 3", "replace_all": True},
                {"old_string": "return 1", "new_string": "return 4", "replace_all": True},
            ],
        },
        ctx,
    )
    assert not res.is_error
    assert "return 3" in edited.read_text()


async def test_edit_missing_file_is_error(ctx):
    res = await EditTool().run({"path": "nope.py", "old_string": "a", "new_string": "b"}, ctx)
    assert res.is_error
    assert "file not found" in res.content


async def test_edit_escape_is_error(ctx):
    res = await EditTool().run(
        {"path": "../outside.py", "old_string": "a", "new_string": "b"}, ctx
    )
    assert res.is_error


async def test_edit_never_raises_on_junk_args(ctx, edited):
    res = await check_tool_contract(EditTool(), {"path": "code.py", "edits": 42}, ctx)
    assert res.is_error


async def test_edit_no_mode_given_is_error(ctx, edited):
    res = await EditTool().run({"path": "code.py"}, ctx)
    assert res.is_error
    assert "old_string or an edits array" in res.content
