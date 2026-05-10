"""Tests for wiki/compile/ — bundle, runner, skill loader."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from wiki import storage
from wiki.compile import (
    DEFAULT_SKILL, CompileResult, compile_wiki, load_skill,
)
from wiki.compile.bundle import build_compile_bundle
from wiki.types import Artifact, Source


def _art(id_, kind, *, area=None, signals=None, body="b", title="t", ts=None):
    return Artifact(
        id=id_, kind=kind, source=Source.BUILTIN,
        title=title, body=body,
        ts=ts or datetime(2025, 1, 1, tzinfo=timezone.utc),
        area=area, signals=signals or {},
    )


# ── Skill loader ──────────────────────────────────────────────────────────────

def test_load_skill_default(tmp_path):
    assert load_skill(tmp_path) == DEFAULT_SKILL


def test_load_skill_user_override(tmp_path):
    sd = storage.skills_dir(tmp_path)
    sd.mkdir(parents=True)
    (sd / "compile.md").write_text("Custom compile skill {budget}", encoding="utf-8")
    assert load_skill(tmp_path) == "Custom compile skill {budget}"


# ── Bundle builder ────────────────────────────────────────────────────────────

def test_bundle_includes_all_kinds(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art("h-1", "hotspot", signals={"path": "x.py", "commits_30d": 10}))
    storage.write_artifact(tmp_path, _art("c-1", "commit", title="fix bug", signals={"is_bugfix": True}))
    storage.write_artifact(tmp_path, _art("p-1", "pr", title="add feature"))
    storage.write_artifact(tmp_path, _art("a-1", "adr", title="ADR-001 use redis"))
    storage.write_artifact(tmp_path, _art("n-1", "note", title="Why X", body="because Y"))
    bundle_str = build_compile_bundle(tmp_path)
    bundle = json.loads(bundle_str)
    assert bundle["counts"]["hotspots"] == 1
    assert bundle["counts"]["commits"] == 1
    assert bundle["counts"]["prs"] == 1
    assert bundle["counts"]["adrs"] == 1
    assert bundle["counts"]["notes"] == 1


def test_bundle_filters_by_area(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art("c-1", "commit", area="payments"))
    storage.write_artifact(tmp_path, _art("c-2", "commit", area="auth"))
    bundle_p = json.loads(build_compile_bundle(tmp_path, area="payments"))
    bundle_a = json.loads(build_compile_bundle(tmp_path, area="auth"))
    assert bundle_p["counts"]["commits"] == 1
    assert bundle_a["counts"]["commits"] == 1
    assert bundle_p["recent_commits"][0]["id"] == "c-1"
    assert bundle_a["recent_commits"][0]["id"] == "c-2"


def test_bundle_empty_repo(tmp_path):
    storage.ensure_layout(tmp_path)
    bundle = json.loads(build_compile_bundle(tmp_path))
    assert all(v == 0 for v in bundle["counts"].values())


def test_bundle_includes_session_insights(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_session_insight(tmp_path, "abc",
        "---\nsource: ratchet\nsession_id: abc\n---\n\n# Insights\n- transactions matter.\n",
    )
    bundle = json.loads(build_compile_bundle(tmp_path))
    assert bundle["counts"]["session_insights"] == 1
    assert "transactions matter" in bundle["session_insights"][0]["body"]
    # Front matter should be stripped.
    assert "source: ratchet" not in bundle["session_insights"][0]["body"]


def test_bundle_truncates_long_bodies(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "n-1", "note", title="Big note", body="X" * 5000,
    ))
    bundle = json.loads(build_compile_bundle(tmp_path))
    body = bundle["notes"][0]["body"]
    assert len(body) <= 700  # 600 cap + "…" suffix


def test_bundle_filters_noisy_hotspots(tmp_path):
    """Bundle must NOT include CHANGELOG / lockfile / *.generated.* / .github
    hotspots — they pollute the LLM's view of where real work happens."""
    storage.ensure_layout(tmp_path)
    # Real signal — should reach the LLM.
    storage.write_artifact(tmp_path, _art(
        "h-real", "hotspot",
        signals={"path": "src/agent.py", "total_changes_30d": 4, "bugfix_count_30d": 2},
    ))
    # Noise — must be dropped before the bundle is built.
    for path, name in [
        ("packages/foo/CHANGELOG.md", "h-changelog"),
        ("package-lock.json", "h-lock"),
        ("packages/ai/src/models.generated.ts", "h-gen"),
        (".github/APPROVED_CONTRIBUTORS", "h-github"),
        ("node_modules/x/index.js", "h-node-modules"),
    ]:
        storage.write_artifact(tmp_path, _art(
            name, "hotspot",
            signals={"path": path, "total_changes_30d": 999},
        ))
    bundle = json.loads(build_compile_bundle(tmp_path))
    paths = [h["signals"]["path"] for h in bundle["hotspots"]]
    assert "src/agent.py" in paths
    assert all("CHANGELOG" not in p and "lock" not in p
               and ".generated." not in p and ".github" not in p
               and "node_modules" not in p for p in paths), \
        f"noise leaked into bundle: {paths}"


def test_bundle_hotspots_ranked_by_real_signal_field(tmp_path):
    """Bundle must rank hotspots by `total_changes_30d` (the real field name).

    Regression test for the historical `commits_30d` typo: hotspots writes
    `total_changes_30d`; the bundle used to read `commits_30d` and got 0
    everywhere → ranking collapsed to alphabetical / arbitrary order.
    """
    storage.ensure_layout(tmp_path)
    # Three hotspots; the real ranking should put 'a' last (lowest churn) and
    # 'c' first (highest churn) by `total_changes_30d`.
    storage.write_artifact(tmp_path, _art(
        "h-a", "hotspot",
        signals={"path": "a.py", "total_changes_30d": 1, "bugfix_count_30d": 0},
    ))
    storage.write_artifact(tmp_path, _art(
        "h-b", "hotspot",
        signals={"path": "b.py", "total_changes_30d": 5, "bugfix_count_30d": 1},
    ))
    storage.write_artifact(tmp_path, _art(
        "h-c", "hotspot",
        signals={"path": "c.py", "total_changes_30d": 20, "bugfix_count_30d": 3},
    ))
    bundle = json.loads(build_compile_bundle(tmp_path))
    paths = [h["signals"]["path"] for h in bundle["hotspots"]]
    # Highest churn first, lowest last.
    assert paths == ["c.py", "b.py", "a.py"]


def test_bundle_keeps_owners_and_recent_fix_signals(tmp_path):
    """Bundle's _KEEP_SIGNAL_KEYS must include the fields renderers / LLMs
    actually need: owners, recent_fix_prs, recent_fix_commits, fix_ratio."""
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "h-1", "hotspot",
        signals={
            "path": "src/x.py",
            "total_changes_30d": 5,
            "bugfix_count_30d": 2,
            "fix_ratio_90d": 0.4,
            "owners": ["Sara", "Marcus"],
            "owners_source": "authors_90d",
            "recent_fix_prs": [101, 102],
            "recent_fix_commits": ["abc123", "def456"],
            "co_changed_files": ["src/y.py"],
        },
    ))
    bundle = json.loads(build_compile_bundle(tmp_path))
    sig = bundle["hotspots"][0]["signals"]
    assert sig["owners"] == ["Sara", "Marcus"]
    assert sig["fix_ratio_90d"] == 0.4
    assert sig["recent_fix_prs"] == [101, 102]
    assert sig["recent_fix_commits"] == ["abc123", "def456"]
    assert sig["co_changed_files"] == ["src/y.py"]


def test_bundle_per_area_includes_commits_now_that_they_have_area(tmp_path):
    """The headline bug from the rate-1-to-9 review: per-area pages were empty
    because commits had area=None. Now that gather tags `area` directly,
    per-area bundles must include them.
    """
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art("c-pay-1", "commit", area="payments", title="fix: refund"))
    storage.write_artifact(tmp_path, _art("c-pay-2", "commit", area="payments", title="feat: void"))
    storage.write_artifact(tmp_path, _art("c-auth-1", "commit", area="auth", title="fix: oauth"))
    storage.write_artifact(tmp_path, _art("p-pay-1", "pr", area="payments", title="payments PR"))
    storage.write_artifact(tmp_path, _art("p-auth-1", "pr", area="auth", title="auth PR"))

    bundle_p = json.loads(build_compile_bundle(tmp_path, area="payments"))
    assert bundle_p["counts"]["commits"] == 2
    assert bundle_p["counts"]["prs"] == 1
    titles = [c["title"] for c in bundle_p["recent_commits"]]
    assert "fix: refund" in titles and "feat: void" in titles


def test_bundle_per_area_finds_multi_area_artifacts_via_signals(tmp_path):
    """A commit spanning two areas has primary `area` = first match but
    `signals.areas` lists both. Per-area filter should find it from EITHER
    area."""
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art(
        "c-multi", "commit", area="auth",   # alphabetically first
        title="cross-area refactor",
        signals={"areas": ["auth", "payments"], "files_changed": ["src/auth/x.py", "src/payments/y.py"]},
    ))
    bundle_auth = json.loads(build_compile_bundle(tmp_path, area="auth"))
    bundle_pay = json.loads(build_compile_bundle(tmp_path, area="payments"))
    assert bundle_auth["counts"]["commits"] == 1
    assert bundle_pay["counts"]["commits"] == 1   # found via signals.areas


# ── Runner with a fake provider ───────────────────────────────────────────────

class _FakeProvider:
    """Returns canned text per call. Use ``per_spec`` to vary by spec name."""

    def __init__(self, response: str = "ok", *, per_spec: dict[str, str] | None = None,
                 error: str | None = None):
        self.response = response
        self.per_spec = per_spec or {}
        self.error = error
        self.calls: list[str] = []

    async def stream(self, model, system, messages, tools, *,
                     signal=None, max_tokens=None, thinking="off"):
        from agent_forge.provider import (
            DoneEvent, StreamErrorEvent, TextDeltaEvent,
        )
        from agent_forge.messages import (
            AssistantMessage, TextContent, TokenUsage,
        )

        # Find which spec this call is for by snooping the user message text.
        user = messages[0].content if messages else ""
        text = self.response
        for k, v in self.per_spec.items():
            if k in user:
                text = v
                break
        self.calls.append(user[:60])

        if self.error is not None:
            yield StreamErrorEvent(error=self.error, retryable=False)
            return

        for ch in text:
            yield TextDeltaEvent(delta=ch)
        msg = AssistantMessage(
            content=(TextContent(text=text),),
            stop_reason="end_turn",
            usage=TokenUsage(input=1, output=2),
        )
        yield DoneEvent(message=msg)


@pytest.mark.asyncio
async def test_compile_writes_global_outputs(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.write_artifact(tmp_path, _art("h-1", "hotspot",
        signals={"path": "src/refund.py", "commits_30d": 5}))
    provider = _FakeProvider(response="# Compiled\n\n- alpha\n")
    from agent_forge.models import DEFAULT_MODEL

    res = await compile_wiki(tmp_path, provider, DEFAULT_MODEL)
    assert isinstance(res, CompileResult)
    assert len(res.errors) == 0
    written_names = {p.name for p in res.written}
    assert "onboarding.md" in written_names
    assert "hotspots.md" in written_names
    assert "adrs.md" in written_names
    # Files exist and contain our canned text.
    onb = storage.curated_dir(tmp_path) / "onboarding.md"
    assert onb.exists()
    body = onb.read_text(encoding="utf-8")
    assert "Compiled" in body
    assert body.startswith("<!-- compiled:")


@pytest.mark.asyncio
async def test_compile_per_area_when_contexts_yaml(tmp_path):
    storage.ensure_layout(tmp_path)
    storage.contexts_path(tmp_path).write_text(
        "areas:\n  payments:\n    paths:\n      - src/payments/**\n",
    )
    provider = _FakeProvider(response="# Page\n")
    from agent_forge.models import DEFAULT_MODEL

    res = await compile_wiki(tmp_path, provider, DEFAULT_MODEL)
    assert (storage.curated_dir(tmp_path) / "per_area" / "payments.md").exists()
    assert (storage.curated_dir(tmp_path) / "per_area" / "payments.md").read_text().startswith("<!--")


@pytest.mark.asyncio
async def test_compile_dry_run_writes_nothing(tmp_path):
    storage.ensure_layout(tmp_path)
    provider = _FakeProvider("would not run")
    from agent_forge.models import DEFAULT_MODEL

    res = await compile_wiki(tmp_path, provider, DEFAULT_MODEL, dry_run=True)
    assert res.written == ()
    assert len(res.skipped) >= 3


@pytest.mark.asyncio
async def test_compile_only_filters_outputs(tmp_path):
    storage.ensure_layout(tmp_path)
    provider = _FakeProvider("# x\n")
    from agent_forge.models import DEFAULT_MODEL

    res = await compile_wiki(tmp_path, provider, DEFAULT_MODEL, only=["onboarding"])
    assert {p.name for p in res.written} == {"onboarding.md"}


@pytest.mark.asyncio
async def test_compile_records_errors(tmp_path):
    storage.ensure_layout(tmp_path)
    provider = _FakeProvider("", error="boom 500")
    from agent_forge.models import DEFAULT_MODEL

    res = await compile_wiki(tmp_path, provider, DEFAULT_MODEL, only=["onboarding"])
    assert res.written == ()
    assert len(res.errors) == 1
    assert "boom 500" in res.errors[0][1]
