"""Tests for wiki/present.py — WIKI section builder."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent_forge.wiki import storage
from agent_forge.wiki.present import build_wiki_section
from agent_forge.wiki.types import Artifact, Source


def _art(id_, kind, *, signals=None, body="b", title="t", ts=None) -> Artifact:
    return Artifact(
        id=id_, kind=kind, source=Source.BUILTIN,
        title=title, body=body,
        ts=ts or datetime(2025, 1, 1, tzinfo=timezone.utc),
        area=None, signals=signals or {},
    )


# ── Empty case ────────────────────────────────────────────────────────────────

def test_returns_none_when_nothing_exists(tmp_path):
    assert build_wiki_section(tmp_path) is None


def test_returns_none_when_layout_present_but_empty(tmp_path):
    storage.ensure_layout(tmp_path)
    # ensure_layout creates raw/cache/.cache so a file exists; but no artifacts.
    out = build_wiki_section(tmp_path)
    # Either None (no artifacts) or whatever fallback path returns; both fine.
    # The contract: never raise, never return empty whitespace.
    assert out is None or out.strip()


# ── Raw-fallback path (no curated/) ───────────────────────────────────────────

def test_raw_fallback_renders_hotspots(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "hot-1", "hotspot",
        signals={"path": "src/refund.py", "total_changes_30d": 12, "bugfix_count_30d": 3, "owners": ["sara"]},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Hot files" in out
    assert "refund.py" in out
    assert "12 changes" in out
    assert "sara" in out
    # Bug regression: the literal "?" must never appear when signal exists.
    assert "? changes" not in out


def test_hotspots_omit_owners_when_empty(tmp_path):
    """Bug fix: don't render 'owners: —' when the gatherer left owners empty."""
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "hot-1", "hotspot",
        signals={"path": "src/refund.py", "total_changes_30d": 5, "owners": []},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "refund.py" in out
    assert "owners" not in out  # noise suppressed when list is empty


def test_hotspots_filter_noisy_paths(tmp_path):
    """CHANGELOG.md, package-lock.json, *.generated.* should never appear in hot files."""
    storage.ensure_layout(tmp_path)
    # Noisy paths with high churn.
    for i, path in enumerate([
        "packages/foo/CHANGELOG.md",
        "package-lock.json",
        "packages/ai/src/models.generated.ts",
        "src/codegen/generated/foo.py",
    ]):
        storage.write_artifact(tmp_path, _art(
            f"noise-{i}", "hotspot",
            signals={"path": path, "total_changes_30d": 999},
        ))
    # One real hot file.
    storage.write_artifact(tmp_path, _art(
        "real", "hotspot",
        signals={"path": "src/agent.py", "total_changes_30d": 4},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "src/agent.py" in out
    assert "CHANGELOG" not in out
    assert "package-lock" not in out
    assert "models.generated" not in out
    assert "/generated/" not in out


def test_raw_fallback_renders_recent_bugfixes(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "c-1", "commit",
        title="fix: refund retry race",
        signals={"is_bugfix": True, "files_changed": ["src/refund.py"]},
        ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
    ))
    storage.write_artifact(tmp_path, _art(
        "c-2", "commit",
        title="feat: add coupons",
        signals={"is_bugfix": False},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Recent bug fixes" in out
    assert "refund retry race" in out
    assert "add coupons" not in out  # non-bugfixes filtered out


def test_raw_fallback_renders_notes(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "note-redis", "note",
        title="Why redis",
        body="We chose redis for the rate limiter because of cluster mode.",
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Notes" in out
    assert "Why redis" in out
    assert "rate limiter" in out


def test_raw_fallback_renders_session_insights(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_session_insight(tmp_path, "abc123", "Always run migrations in a transaction.\n")
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "session insights" in out.lower()
    assert "transaction" in out


# ── Curated path (preferred when present) ─────────────────────────────────────

def test_curated_overrides_raw(tmp_path):
    storage.ensure_layout(tmp_path)
    # Add raw signal that would otherwise show up.
    storage.write_artifact(tmp_path, _art(
        "hot-1", "hotspot",
        signals={"path": "src/refund.py", "total_changes_30d": 99},
    ))
    # Now add curated/ — it should take over.
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "onboarding.md").write_text("# Onboarding\nStart with payments.", encoding="utf-8")
    (cdir / "hotspots.md").write_text("# Hot\n- billing.py: ours.", encoding="utf-8")

    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Start with payments" in out
    assert "billing.py" in out
    # Raw hotspot text should NOT appear (curated takes precedence).
    assert "src/refund.py" not in out


def test_curated_per_area_files(tmp_path):
    storage.ensure_layout(tmp_path)
    cdir = storage.curated_dir(tmp_path)
    (cdir / "per_area").mkdir(parents=True, exist_ok=True)
    (cdir / "per_area" / "payments.md").write_text("payments narrative", encoding="utf-8")
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "payments" in out
    assert "payments narrative" in out


# ── Budget enforcement ────────────────────────────────────────────────────────

def test_budget_is_enforced(tmp_path):
    storage.ensure_layout(tmp_path)
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "onboarding.md").write_text("X" * 50_000, encoding="utf-8")
    (cdir / "hotspots.md").write_text("Y" * 50_000, encoding="utf-8")
    out = build_wiki_section(tmp_path, budget=2000)
    assert out is not None
    assert len(out) <= 2200  # header overhead allowance


def test_zero_budget_returns_none_or_minimal(tmp_path):
    storage.ensure_layout(tmp_path)
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "onboarding.md").write_text("hi", encoding="utf-8")
    out = build_wiki_section(tmp_path, budget=10)
    # With budget=10 the header alone exceeds — should still not crash.
    assert out is None or len(out) < 200


# ── Error tolerance ───────────────────────────────────────────────────────────

# ── Project conventions / reverts / notable PRs (new sections) ─────────────────────

def test_repo_file_artifacts_become_conventions_section(tmp_path):
    """AGENTS.md gathered as a repo_file should appear as 'Project conventions'."""
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "repo_file-AGENTS.md", "repo_file",
        title="AGENTS.md",
        body="# Development Rules\n\n## Code Quality\n\n- No `any` types unless absolutely necessary\n- Always run npm run check after edits\n",
        signals={"path": "AGENTS.md"},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Project conventions" in out
    assert "AGENTS.md" in out
    assert "No `any` types" in out


def test_repo_file_skeleton_keeps_all_section_headers(tmp_path):
    """Skeleton extraction preserves every ## heading even if bodies are huge."""
    storage.ensure_layout(tmp_path)
    body = (
        "# Title\n\n"
        "## Section A\n\n"
        "- bullet a1\n- bullet a2\n- bullet a3\n- bullet a4 (should be dropped)\n\n"
        + ("filler line\n" * 50)
        + "\n## Section B\n\n"
        "- bullet b1\n- bullet b2\n\n"
        "## Section C\n\nplain prose line one\nplain prose line two\nthird line dropped\n\n"
        "## Section D\n\n```\ncode block ignored\n```\n\nfinal text\n"
    )
    storage.write_artifact(tmp_path, _art(
        "repo_file-AGENTS.md", "repo_file",
        title="AGENTS.md",
        body=body,
        signals={"path": "AGENTS.md"},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    # Every section header survives.
    assert "Section A" in out
    assert "Section B" in out
    assert "Section C" in out
    assert "Section D" in out
    # Bullet budget is 3 — the 4th bullet under A should be gone.
    assert "bullet a3" in out
    assert "bullet a4" not in out
    # Code blocks never appear in the skeleton.
    assert "code block ignored" not in out
    # Skeleton marker tells the user the source was compressed.
    assert "skeleton" in out.lower()


def test_reverts_appear_as_their_own_section(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "revert-1", "revert",
        title='Revert "feat(ai): add experimental codex provider (#730)"',
        signals={"is_revert": True, "reverts_sha": "abc123"},
        ts=datetime(2026, 1, 14, tzinfo=timezone.utc),
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Recently reverted" in out
    assert "experimental codex" in out
    # The redundant 'Revert ' prefix should be stripped under the header.
    assert 'Revert "' not in out


def test_hotspots_grouped_by_area_when_contexts_yaml_present(tmp_path):
    """When contexts.yaml declares areas, hot files are grouped per-area."""
    storage.ensure_layout(tmp_path)
    # Write a contexts.yaml.
    storage.contexts_path(tmp_path).write_text(
        "areas:\n"
        "  payments:\n"
        "    paths:\n"
        "      - src/payments/**\n"
        "  auth:\n"
        "    paths:\n"
        "      - src/auth/**\n",
        encoding="utf-8",
    )
    # Hotspots in two areas plus one uncategorised.
    storage.write_artifact(tmp_path, _art(
        "hot-pay", "hotspot",
        signals={"path": "src/payments/refund.py", "total_changes_30d": 12},
    ))
    storage.write_artifact(tmp_path, _art(
        "hot-auth", "hotspot",
        signals={"path": "src/auth/oauth.py", "total_changes_30d": 8},
    ))
    storage.write_artifact(tmp_path, _art(
        "hot-misc", "hotspot",
        signals={"path": "src/misc/util.py", "total_changes_30d": 4},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Hot files by area" in out         # per-area header used
    assert "**payments**" in out
    assert "**auth**" in out
    assert "**(other)**" in out               # uncategorised bucket
    assert "refund.py" in out and "oauth.py" in out and "util.py" in out


def test_hotspots_filter_includes_github_and_node_modules(tmp_path):
    storage.ensure_layout(tmp_path)
    for i, path in enumerate([
        ".github/APPROVED_CONTRIBUTORS",
        ".github/workflows/ci.yml",
        "node_modules/foo/index.js",
        "dist/bundle.js",
    ]):
        storage.write_artifact(tmp_path, _art(
            f"noise-{i}", "hotspot",
            signals={"path": path, "total_changes_30d": 999},
        ))
    storage.write_artifact(tmp_path, _art(
        "real", "hotspot",
        signals={"path": "src/agent.py", "total_changes_30d": 4},
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "src/agent.py" in out
    assert ".github/" not in out
    assert "node_modules" not in out
    assert "dist/" not in out


def test_notable_prs_ranked_by_discussion(tmp_path):
    storage.ensure_layout(tmp_path)
    # Quiet PR — should NOT appear (score < 2).
    storage.write_artifact(tmp_path, _art(
        "pr-quiet", "pr", title="chore: bump deps",
        signals={"review_comments": [], "inline_comments": [], "issue_comments": []},
    ))
    # Discussed PR — should appear.
    storage.write_artifact(tmp_path, _art(
        "pr-loud", "pr", title="feat: rewrite scheduler",
        signals={
            "review_comments": [{"author": "a", "body": "b", "state": "COMMENTED"}] * 4,
            "inline_comments": [{"author": "c", "body": "d"}] * 2,
            "issue_comments": [],
            "change_requests": ["reviewer-x"],
        },
    ))
    out = build_wiki_section(tmp_path)
    assert out is not None
    assert "Notable PRs" in out
    assert "rewrite scheduler" in out
    assert "chore: bump deps" not in out


# ── Error tolerance ────────────────────────────────────────────────────────────

def test_corrupt_curated_files_dont_crash(tmp_path):
    storage.ensure_layout(tmp_path)
    cdir = storage.curated_dir(tmp_path)
    cdir.mkdir(parents=True, exist_ok=True)
    # Binary garbage where a markdown file is expected.
    (cdir / "onboarding.md").write_bytes(b"\x00\x01\x02\xff\xfe")
    # Should not raise.
    out = build_wiki_section(tmp_path)
    # Either None or some recovered content; the contract is "don't crash".
    assert out is None or isinstance(out, str)
