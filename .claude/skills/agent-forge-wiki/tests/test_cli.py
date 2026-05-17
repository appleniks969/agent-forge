"""Smoke tests for the wiki CLI subcommand."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from wiki import storage
from wiki.gather import cli as wiki_cli
from wiki.gather import discovery
from wiki.types import Artifact, Gatherer, Source


class _FakeG(Gatherer):
    name = "fake"

    async def gather(self, repo_root, since, cursor):
        return [Artifact(
            id="x-1", kind="note", source=Source.BUILTIN,
            title="t", body="b", ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )]


def test_cli_main_runs_gather_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeG,))
    rc = wiki_cli._main(["gather", "--cwd", str(tmp_path), "--quiet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "+1 artifacts" in out


def test_cli_main_status_when_empty(tmp_path, capsys):
    rc = wiki_cli._main(["status", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no .agent-forge/raw/" in out


def test_cli_main_status_after_gather(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeG,))
    wiki_cli._main(["gather", "--cwd", str(tmp_path), "--quiet"])
    capsys.readouterr()  # clear
    rc = wiki_cli._main(["status", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "notes" in out


def test_cli_rejects_bad_since(tmp_path):
    with pytest.raises(SystemExit) as exc:
        wiki_cli._main(["gather", "--cwd", str(tmp_path), "--since", "not-a-date"])
    assert exc.value.code == 2


def test_cli_main_handles_only_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeG,))
    rc = wiki_cli._main([
        "gather", "--cwd", str(tmp_path), "--only", "fake", "--quiet",
    ])
    assert rc == 0


# ── wiki init: scaffold contexts.yaml ────────────────────────────────────────

def test_init_detects_packages_monorepo(tmp_path, capsys):
    (tmp_path / "packages" / "ai").mkdir(parents=True)
    (tmp_path / "packages" / "agent").mkdir(parents=True)
    (tmp_path / "packages" / "tui").mkdir(parents=True)

    rc = wiki_cli._main(["init", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "detected 3 area(s)" in out
    written = (tmp_path / ".agent-forge" / "contexts.yaml").read_text()
    assert "areas:" in written
    assert "  ai:" in written
    assert "packages/ai/**" in written
    assert "  agent:" in written
    assert "  tui:" in written

    # And it's parseable by load_contexts.
    areas, _ = storage.load_contexts(tmp_path)
    assert set(areas) == {"ai", "agent", "tui"}
    assert "packages/ai/**" in areas["ai"]


def test_init_falls_back_to_src_layout(tmp_path):
    (tmp_path / "src" / "payments").mkdir(parents=True)
    (tmp_path / "src" / "auth").mkdir(parents=True)

    rc = wiki_cli._main(["init", "--cwd", str(tmp_path)])
    assert rc == 0
    written = (tmp_path / ".agent-forge" / "contexts.yaml").read_text()
    assert "src/payments/**" in written
    assert "src/auth/**" in written


def test_init_falls_back_to_top_level_dirs(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "node_modules").mkdir()  # blacklisted — must NOT appear
    (tmp_path / ".git").mkdir()           # blacklisted — must NOT appear

    rc = wiki_cli._main(["init", "--cwd", str(tmp_path)])
    assert rc == 0
    written = (tmp_path / ".agent-forge" / "contexts.yaml").read_text()
    assert "frontend/**" in written
    assert "backend/**" in written
    assert "node_modules" not in written
    assert ".git" not in written


def test_init_refuses_to_overwrite_existing_file(tmp_path, capsys):
    (tmp_path / ".agent-forge").mkdir()
    target = tmp_path / ".agent-forge" / "contexts.yaml"
    target.write_text("# user-edited\n", encoding="utf-8")

    rc = wiki_cli._main(["init", "--cwd", str(tmp_path)])
    assert rc == 1
    assert "refusing to overwrite" in capsys.readouterr().out
    # Original content untouched.
    assert target.read_text() == "# user-edited\n"


def test_init_force_overwrites(tmp_path):
    (tmp_path / ".agent-forge").mkdir()
    (tmp_path / "packages" / "x").mkdir(parents=True)
    target = tmp_path / ".agent-forge" / "contexts.yaml"
    target.write_text("# old\n", encoding="utf-8")

    rc = wiki_cli._main(["init", "--cwd", str(tmp_path), "--force"])
    assert rc == 0
    assert "areas:" in target.read_text()
    assert "old" not in target.read_text()


# ── wiki status: actionable suggestions ──────────────────────────────────────

def test_status_warns_when_no_contexts_yaml(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeG,))
    wiki_cli._main(["gather", "--cwd", str(tmp_path), "--quiet"])
    capsys.readouterr()
    rc = wiki_cli._main(["status", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Suggestions:" in out
    assert "wiki init" in out


def test_status_no_warning_when_contexts_yaml_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discovery, "BUILTINS", (_FakeG,))
    wiki_cli._main(["gather", "--cwd", str(tmp_path), "--quiet"])
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  app:\n    paths:\n      - src/**\n", encoding="utf-8",
    )
    capsys.readouterr()
    rc = wiki_cli._main(["status", "--cwd", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wiki init" not in out
