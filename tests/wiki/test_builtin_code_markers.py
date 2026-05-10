"""Tests for code_markers gatherer — both rg path and Python fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agent_forge._subprocess import Completed
from agent_forge.wiki.gather.builtin import code_markers


@pytest.mark.asyncio
async def test_python_scan_finds_todo(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n# TODO: refactor this\ny = 2\n")
    out = code_markers._python_scan(tmp_path)
    assert any("TODO" in line for _, line in next(iter(out.values())))


@pytest.mark.asyncio
async def test_marker_regex_catches_scoped_styles(tmp_path):
    """Regression: the old ``\\b(TODO|...)\\b[: ]`` pattern silently dropped
    the common ``TODO(scope):`` and ``FIXME[area]:`` styles because the
    char immediately after the keyword was ``(`` / ``[`` rather than ``:`` or
    space. The loosened regex must catch all of these."""
    src = (
        "// TODO(payments): refactor charge flow\n"      # paren-scoped
        "// FIXME[auth]: token leak on retry\n"          # bracket-scoped
        "/* HACK. legacy thing */\n"                     # period-suffixed
        "// XXX-temp: remove before launch\n"            # dash-suffixed
        "/* NOTE\n"                                       # EOL-terminated
        "function clean() { /* no marker here */ }\n"    # control: no match
        "const TODOlist = [];\n"                          # control: word boundary still rejects
    )
    (tmp_path / "a.ts").write_text(src)
    out = code_markers._python_scan(tmp_path)
    assert len(out) == 1
    matches = next(iter(out.values()))
    bodies = {body for _, body in matches}
    # All five real markers should land:
    assert any("TODO(payments)" in b for b in bodies)
    assert any("FIXME[auth]" in b for b in bodies)
    assert any("HACK." in b for b in bodies)
    assert any("XXX-temp" in b for b in bodies)
    assert any("NOTE" in b and "TODOlist" not in b for b in bodies)
    # And TODOlist (no word-boundary marker) should NOT have produced its own match line:
    assert not any("TODOlist" in b for b in bodies)
    # Function comment should not match:
    assert not any("no marker here" in b for b in bodies)


@pytest.mark.asyncio
async def test_python_scan_skips_skipdirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.py").write_text("# TODO: ignore me")
    (tmp_path / "ok.py").write_text("# TODO: keep me")
    out = code_markers._python_scan(tmp_path)
    paths = {str(p.relative_to(tmp_path)) for p in out}
    assert "ok.py" in paths
    assert ".git/x.py" not in paths


@pytest.mark.asyncio
async def test_python_scan_skips_non_source_extensions(tmp_path):
    (tmp_path / "data.csv").write_text("# TODO: ignore me")
    (tmp_path / "main.py").write_text("# FIXME: keep")
    out = code_markers._python_scan(tmp_path)
    assert all(p.suffix == ".py" for p in out)


@pytest.mark.asyncio
async def test_gather_emits_artifact_per_file(tmp_path):
    (tmp_path / "a.py").write_text("# TODO: refactor\n# FIXME: leak\n")
    (tmp_path / "b.py").write_text("clean\n")
    # Force the python fallback by patching shutil.which to return None.
    with patch.object(code_markers.shutil, "which", return_value=None):
        g = code_markers.CodeMarkersGatherer()
        arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    titles = {a.title for a in arts}
    assert "a.py" in titles
    assert "b.py" not in titles


@pytest.mark.asyncio
async def test_gather_uses_rg_when_present(tmp_path):
    rg_output = (
        f"{tmp_path}/foo.py:3:    # TODO: do thing\n"
        f"{tmp_path}/foo.py:7:    # FIXME: bug\n"
        f"{tmp_path}/bar.py:1:    # noqa\n"
    )
    fake_completed = Completed(returncode=0, stdout=rg_output, stderr="")

    async def fake_run(*args, **kwargs):
        return fake_completed

    with patch.object(code_markers.shutil, "which", return_value="/usr/bin/rg"), \
         patch.object(code_markers, "run_subprocess", fake_run):
        g = code_markers.CodeMarkersGatherer()
        arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    titles = {a.title for a in arts}
    assert "foo.py" in titles and "bar.py" in titles
    foo = next(a for a in arts if a.title == "foo.py")
    assert foo.signals["marker_count"] == 2
