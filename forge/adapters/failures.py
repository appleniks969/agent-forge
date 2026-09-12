"""Failure fold: derive lessons from session logs without touching the kernel.

Layer: adapters — kernel serde + ports only. The session JSONL remains the
source of truth. This module is a separate program in the DESIGN §1 sense:
it reads envelopes, appends derived facts, and projects a byte-budgeted
markdown file. Nothing here is a kernel event.

Trust: tool stderr and denied permissions are FIRM; user corrections are
HUMAN. Model self-talk is not extracted. Fact text is a short template plus
a redacted first line — never the raw tool body — so a poisoned ToolFinished
cannot become an instruction.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from forge.adapters.jsonl_store import load_index, read_log
from forge.adapters.redact import redact_text
from forge.kernel.events import (
    Envelope,
    PermissionDecided,
    ToolDeclared,
    ToolFinished,
    TurnFinished,
    UserSubmitted,
)

Trust = Literal["firm", "human"]
Kind = Literal["tool_error", "turn", "denial", "correction"]

FACTS_NAME = "failures.jsonl"
CURSOR_NAME = "failures.cursor.json"
WIKI_NAME = "failures.md"
PROJECTION_CAP = 4_000  # bytes, not tokens — the prompt thunk's budget
SIGNATURE_MAX = 120
_INJECTION = re.compile(
    r"(?i)(ignore (all )?(previous|prior) (instructions|prompts)|"
    r"you are now|system prompt|exfiltrat|you must\b)"
)
_REMEMBER_INJECT = re.compile(r"(?i)^remember (to|that)\b")
_CORRECTION = re.compile(
    r"(?i)^(no\b|don't\b|do not\b|never\b|always\b|use\b|remember\b|stop\b)"
)


@dataclass(frozen=True)
class FailureFact:
    id: str
    ts: float
    kind: Kind
    trust: Trust
    text: str
    derived_from: tuple[str, ...]
    tool: str | None = None
    signature: str = ""
    cwd: str | None = None
    supersedes: str | None = None

    def cite(self) -> str:
        return ", ".join(self.derived_from)


def facts_path(project: Path) -> Path:
    return Path(project) / ".agent-forge" / FACTS_NAME


def cursor_path(project: Path) -> Path:
    return Path(project) / ".agent-forge" / CURSOR_NAME


def wiki_path(project: Path) -> Path:
    return Path(project) / ".agent-forge" / "wiki" / WIKI_NAME


def fact_id(kind: str, tool: str | None, signature: str) -> str:
    raw = f"{kind}|{tool or ''}|{signature}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _first_line(text: str, *, scrub_remember: bool = True) -> str:
    line = (text.splitlines() or [""])[0].strip()
    line = redact_text(line)
    if _INJECTION.search(line) or (scrub_remember and _REMEMBER_INJECT.match(line)):
        return "[redacted-injection]"
    return line[:SIGNATURE_MAX]


def _looks_like_correction(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 200:
        return False
    return bool(_CORRECTION.match(stripped))


def extract_from_envelopes(
    envelopes: Iterable[Envelope],
    *,
    sid: str,
    cwd: str | None,
    after_seq: int = -1,
) -> list[FailureFact]:
    """Pure fold: envelopes -> new facts for seq > after_seq.

    Events at or below after_seq still update the running prev-error pointer
    so a correction on the next user turn is attributed correctly after a
    resume.
    """
    facts: list[FailureFact] = []
    prev_error: Envelope | None = None
    tool_names: dict[str, str] = {}

    for env in envelopes:
        body = env.body
        if isinstance(body, ToolDeclared):
            tool_names[body.call.id] = body.call.name
        cite = f"{sid}:{env.seq}"
        ts = env.ts or time.time()

        if isinstance(body, ToolFinished) and body.result.is_error:
            tool = tool_names.get(body.result.call_id, "tool")
            sig = _first_line(body.result.content)
            if env.seq > after_seq:
                facts.append(
                    FailureFact(
                        id=fact_id("tool_error", tool, sig),
                        ts=ts,
                        kind="tool_error",
                        trust="firm",
                        text=f"{tool} failed: {sig}" if sig else f"{tool} failed",
                        derived_from=(cite,),
                        tool=tool,
                        signature=sig,
                        cwd=cwd,
                    )
                )
            prev_error = env
            continue

        if isinstance(body, TurnFinished) and body.outcome != "ok":
            sig = body.outcome
            if env.seq > after_seq:
                facts.append(
                    FailureFact(
                        id=fact_id("turn", None, sig),
                        ts=ts,
                        kind="turn",
                        trust="firm",
                        text=f"turn ended {body.outcome}",
                        derived_from=(cite,),
                        signature=sig,
                        cwd=cwd,
                    )
                )
            prev_error = env
            continue

        if isinstance(body, PermissionDecided) and not body.allowed:
            sig = f"{body.source}:{body.reason or 'denied'}"
            if env.seq > after_seq:
                facts.append(
                    FailureFact(
                        id=fact_id("denial", None, sig),
                        ts=ts,
                        kind="denial",
                        trust="firm",
                        text=(
                            f"permission denied ({body.source}): "
                            f"{body.reason or 'denied'}"
                        ),
                        derived_from=(cite,),
                        signature=sig,
                        cwd=cwd,
                    )
                )
            continue

        if isinstance(body, UserSubmitted):
            if (
                prev_error is not None
                and _looks_like_correction(body.text)
                and env.seq > after_seq
            ):
                sig = _first_line(body.text, scrub_remember=False)
                facts.append(
                    FailureFact(
                        id=fact_id("correction", None, sig),
                        ts=ts,
                        kind="correction",
                        trust="human",
                        text=f"user correction: {sig}",
                        derived_from=(cite, f"{sid}:{prev_error.seq}"),
                        signature=sig,
                        cwd=cwd,
                    )
                )
            prev_error = None

    return facts


def load_facts(project: Path) -> list[FailureFact]:
    path = facts_path(project)
    if not path.exists():
        return []
    out: list[FailureFact] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            out.append(_fact_from_dict(d))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fact_from_dict(d: Mapping[str, Any]) -> FailureFact:
    derived = d.get("derived_from") or ()
    return FailureFact(
        id=str(d["id"]),
        ts=float(d.get("ts") or 0),
        kind=d["kind"],  # type: ignore[arg-type]
        trust=d.get("trust", "firm"),  # type: ignore[arg-type]
        text=str(d["text"]),
        derived_from=tuple(derived),
        tool=d.get("tool"),
        signature=str(d.get("signature") or ""),
        cwd=d.get("cwd"),
        supersedes=d.get("supersedes"),
    )


def load_cursor(project: Path) -> dict[str, int]:
    path = cursor_path(project)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def _write_cursor(project: Path, cursor: Mapping[str, int]) -> None:
    path = cursor_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(cursor), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def append_facts(project: Path, facts: Iterable[FailureFact]) -> int:
    """Append facts, skipping ids already live. Returns number written."""
    existing = {f.id for f in live_facts(load_facts(project))}
    new = [f for f in facts if f.id not in existing]
    if not new:
        return 0
    path = facts_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for fact in new:
            rec = asdict(fact)
            rec["derived_from"] = list(fact.derived_from)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(new)


def live_facts(facts: Iterable[FailureFact]) -> list[FailureFact]:
    """Last write per id, minus ids that a later fact supersedes."""
    by_id: dict[str, FailureFact] = {}
    superseded: set[str] = set()
    for fact in facts:
        by_id[fact.id] = fact
        if fact.supersedes:
            superseded.add(fact.supersedes)
    return [f for f in by_id.values() if f.id not in superseded]


def project_markdown(facts: Iterable[FailureFact], *, cap: int = PROJECTION_CAP) -> str:
    live = sorted(live_facts(facts), key=lambda f: f.ts, reverse=True)
    if not live:
        return ""
    lines = ["# Failures", ""]
    for fact in live:
        cite = fact.cite()
        lines.append(f"- ({fact.trust}) {fact.text} [{cite}]")
    text = "\n".join(lines) + "\n"
    if len(text.encode()) <= cap:
        return text
    kept = lines[:2]
    body = lines[2:]
    while body:
        candidate = "\n".join(kept + body) + "\n"
        if len(candidate.encode()) <= cap:
            return candidate
        body.pop()
    return "# Failures\n"


def write_projection(project: Path, facts: Iterable[FailureFact] | None = None) -> Path:
    if facts is None:
        facts = load_facts(project)
    text = project_markdown(facts)
    path = wiki_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sync_one(sessions_root: Path, project: Path, cwd: str) -> dict[str, int]:
    project = Path(project)
    cursor = load_cursor(project)
    index = load_index(sessions_root)
    added = 0
    scanned = 0
    for sid, meta in index.items():
        if meta.get("cwd") != cwd:
            continue
        last_seq = int(meta.get("last_seq", -1))
        if last_seq <= cursor.get(sid, -1):
            continue
        path = Path(meta.get("path") or Path(sessions_root) / f"{sid}.jsonl")
        try:
            envs = read_log(path)
        except (OSError, ValueError):
            continue
        scanned += 1
        after = cursor.get(sid, -1)
        new = extract_from_envelopes(envs, sid=sid, cwd=cwd, after_seq=after)
        added += append_facts(project, new)
        if envs:
            cursor[sid] = max(e.seq for e in envs)
    _write_cursor(project, cursor)
    write_projection(project)
    return {
        "scanned": scanned,
        "added": added,
        "live": len(live_facts(load_facts(project))),
    }


def project_cwds(sessions_root: Path, fallback: str | None = None) -> list[str]:
    """Distinct index cwds, plus fallback if it has no sessions yet."""
    cwds = sorted(
        {str(e["cwd"]) for e in load_index(sessions_root).values() if e.get("cwd")}
    )
    if fallback and fallback not in cwds:
        cwds.append(fallback)
    return cwds


def sync_failures(
    sessions_root: Path,
    project: Path,
    *,
    cwd: str | None = None,
    all_dirs: bool = False,
) -> dict[str, int]:
    """Incrementally fold new envelopes into facts + wiki. Returns counts.

    Project-scoped by default (index cwd must match). ``all_dirs`` folds each
    known cwd into *that* project's store so a foreign lesson never lands in
    this wiki.
    """
    project = Path(project)
    cwd = cwd if cwd is not None else str(project)
    if not all_dirs:
        return _sync_one(sessions_root, project, cwd)
    totals = {"scanned": 0, "added": 0, "live": 0}
    for other in project_cwds(sessions_root, fallback=cwd):
        stats = _sync_one(sessions_root, Path(other), other)
        for key in totals:
            totals[key] += stats[key]
    return totals


def read_projection(project: Path) -> str | None:
    path = wiki_path(project)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def facts_as_dicts(facts: Iterable[FailureFact]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fact in facts:
        rec = asdict(fact)
        rec["derived_from"] = list(fact.derived_from)
        out.append(rec)
    return out
