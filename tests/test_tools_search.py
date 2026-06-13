"""GrepTool/FindTool: fallback behavior, containment, and rg/fd parity on a fixture tree."""

from __future__ import annotations

import asyncio
import os
import shutil
import time

import pytest

from forge.adapters.tools.search import FindTool, GrepTool
from forge.adapters.tools.workspace import RootedWorkspace
from forge.ports.tool import ToolCtx

RG = shutil.which("rg") is not None
FD = shutil.which("fd") is not None


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("def alpha():\n    return 'needle'\n")
    (tmp_path / "src" / "beta.txt").write_text("needle in text\nno match here\nNEEDLE upper\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# guide\nfind the needle\n")
    (tmp_path / "top.py").write_text("print('top')\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "h.txt").write_text("needle hidden\n")
    return tmp_path


@pytest.fixture
def ctx(tree):
    return ToolCtx(ws=RootedWorkspace(tree), cancel=asyncio.Event())


def lines(res):
    return res.content.splitlines()


# --- GrepTool fallback ---------------------------------------------------------


async def test_fallback_grep_finds_matches(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle"}, ctx)
    assert not res.is_error
    got = set(lines(res))
    assert "src/alpha.py:2:    return 'needle'" in got
    assert "src/beta.txt:1:needle in text" in got
    assert "docs/guide.md:2:find the needle" in got
    assert not any("NEEDLE" in line for line in got)  # case-sensitive by default


async def test_fallback_grep_skips_hidden(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle"}, ctx)
    assert not any(".hidden" in line for line in lines(res))


async def test_fallback_grep_case_insensitive(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle", "case_insensitive": True}, ctx)
    assert "src/beta.txt:3:NEEDLE upper" in set(lines(res))


async def test_fallback_grep_glob_filter(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle", "glob": "**/*.py"}, ctx)
    got = lines(res)
    assert got == ["src/alpha.py:2:    return 'needle'"]


async def test_fallback_grep_scoped_path(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle", "path": "docs"}, ctx)
    assert lines(res) == ["docs/guide.md:2:find the needle"]


async def test_fallback_grep_single_file_target(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "needle", "path": "src/beta.txt"}, ctx)
    assert lines(res) == ["src/beta.txt:1:needle in text"]


async def test_grep_no_matches(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "zzz_nothing"}, ctx)
    assert not res.is_error
    assert res.content == "(no matches)"


async def test_grep_invalid_regex_is_error(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "(unclosed"}, ctx)
    assert res.is_error
    assert "invalid regex" in res.content


async def test_grep_escape_is_error(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "x", "path": ".."}, ctx)
    assert res.is_error


async def test_grep_missing_path_is_error(ctx):
    res = await GrepTool(use_rg=False).run({"pattern": "x", "path": "nope"}, ctx)
    assert res.is_error


async def test_grep_cancelled_returns_aborted(ctx):
    ctx.cancel.set()
    res = await GrepTool(use_rg=False).run({"pattern": "needle"}, ctx)
    assert res.is_error
    assert "abort" in res.content.lower()


# --- rg parity -------------------------------------------------------------------


@pytest.mark.skipif(not RG, reason="rg not installed")
@pytest.mark.parametrize(
    "args",
    [
        {"pattern": "needle"},
        {"pattern": "needle", "case_insensitive": True},
        {"pattern": "needle", "glob": "**/*.py"},
        {"pattern": "needle", "path": "docs"},
        {"pattern": "needle", "path": "src/beta.txt"},
        {"pattern": "zzz_nothing"},
    ],
)
async def test_rg_and_fallback_parity(ctx, args):
    via_rg = await GrepTool(use_rg=True).run(dict(args), ctx)
    via_py = await GrepTool(use_rg=False).run(dict(args), ctx)
    assert not via_rg.is_error and not via_py.is_error
    assert set(lines(via_rg)) == set(lines(via_py))


# --- FindTool --------------------------------------------------------------------


async def test_find_sorted_by_mtime_newest_first(ctx, tree):
    now = time.time()
    os.utime(tree / "src" / "alpha.py", (now - 100, now - 100))
    os.utime(tree / "top.py", (now, now))
    res = await FindTool(use_fd=False).run({"pattern": "**/*.py"}, ctx)
    assert not res.is_error
    assert lines(res) == ["top.py", "src/alpha.py"]


async def test_find_scoped_path(ctx):
    res = await FindTool(use_fd=False).run({"pattern": "*.txt", "path": "src"}, ctx)
    assert lines(res) == ["src/beta.txt"]


async def test_find_no_matches(ctx):
    res = await FindTool(use_fd=False).run({"pattern": "**/*.zig"}, ctx)
    assert not res.is_error
    assert res.content == "(no files found)"


async def test_find_missing_pattern_is_error(ctx):
    res = await FindTool(use_fd=False).run({}, ctx)
    assert res.is_error


async def test_find_escape_is_error(ctx):
    res = await FindTool(use_fd=False).run({"pattern": "*", "path": "../.."}, ctx)
    assert res.is_error


async def test_find_skips_hidden(ctx):
    res = await FindTool(use_fd=False).run({"pattern": "**/*.txt"}, ctx)
    assert not any(".hidden" in line for line in lines(res))


@pytest.mark.skipif(not FD, reason="fd not installed")
async def test_fd_and_fallback_parity(ctx):
    via_fd = await FindTool(use_fd=True).run({"pattern": "**/*.py"}, ctx)
    via_py = await FindTool(use_fd=False).run({"pattern": "**/*.py"}, ctx)
    assert not via_fd.is_error and not via_py.is_error
    assert set(lines(via_fd)) == set(lines(via_py))
