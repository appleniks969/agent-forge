"""Tests for the repo_files gatherer — README, ADRs, CODEOWNERS parsing."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wiki.gather.builtin.repo_files import (
    RepoFilesGatherer, _parse_codeowners,
)


def test_parse_codeowners_extracts_patterns():
    text = """
# top-level
*           @org/eng
src/payments/**  @org/payments-team @sara
docs/         @org/docs
"""
    out = _parse_codeowners(text)
    assert out["*"] == ["@org/eng"]
    assert out["src/payments/**"] == ["@org/payments-team", "@sara"]


@pytest.mark.asyncio
async def test_repo_files_gatherer_picks_up_root_files(tmp_path):
    (tmp_path / "README.md").write_text("# my project\n")
    (tmp_path / "CHANGELOG.md").write_text("# 1.0\n")
    (tmp_path / "AGENTS.md").write_text("# agents")

    g = RepoFilesGatherer()
    arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    titles = {a.title for a in arts}
    assert "README.md" in titles
    assert "CHANGELOG.md" in titles
    assert "AGENTS.md" in titles


@pytest.mark.asyncio
async def test_repo_files_gatherer_picks_up_adrs(tmp_path):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-record-architecture-decisions.md").write_text("# ADR 1")
    (adr_dir / "0002-use-redis.md").write_text("# ADR 2")

    g = RepoFilesGatherer()
    arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    adrs = [a for a in arts if a.kind == "adr"]
    assert len(adrs) == 2
    assert all(a.signals.get("path", "").startswith("docs/adr/") for a in adrs)


@pytest.mark.asyncio
async def test_repo_files_gatherer_parses_codeowners_into_signals(tmp_path):
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "CODEOWNERS").write_text("src/payments/**  @sara\n")
    g = RepoFilesGatherer()
    arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    co_arts = [a for a in arts if a.signals.get("codeowners")]
    assert co_arts, "expected at least one artifact with parsed CODEOWNERS"
    assert "src/payments/**" in co_arts[0].signals["codeowners"]


@pytest.mark.asyncio
async def test_repo_files_gatherer_returns_empty_on_bare_dir(tmp_path):
    g = RepoFilesGatherer()
    out = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    assert out == []


@pytest.mark.asyncio
async def test_repo_files_gatherer_caps_large_body(tmp_path):
    (tmp_path / "README.md").write_text("x" * 300_000)
    g = RepoFilesGatherer()
    arts = await g.gather(tmp_path, datetime(2020, 1, 1, tzinfo=timezone.utc), {})
    a = next(a for a in arts if a.title == "README.md")
    assert "truncated" in a.body
