"""
session.py — JSONL append-only session log + memory.md read/write.

Depends only on provider. Sibling to context.py: context owns in-memory window
state; session owns on-disk state. Neither imports the other. The loop is
deliberately excluded — loop stays pure; chat.py calls both loop and session.

Session JSONL format (one JSON object per line):
  {"type":"metadata",   "id":"...", "model":"...", "cwd":"...", "ts":...}
  {"type":"message",    "id":"...", "ts":..., "message":{...}, "usage":{...}}
  {"type":"compaction", "id":"...", "ts":..., "summary":"...",
   "first_kept_id":"...", "tokens_before":N, "tokens_after":N}

Memory format: ~/.agent-forge/memory.md (global) + <cwd>/.agent-forge/memory.md
(project), merged and deduped on load. Entries are bullet lines with a
`(learned YYYY-MM-DD)` stamp; capped at ~2 K tokens (~8 KB) by evicting the
oldest bullets first.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .messages import (
    AssistantMessage, ImageContent, Message, TextContent, ThinkingContent,
    ToolCallContent, ToolResultMessage, TokenUsage, UserMessage, ZERO_USAGE,
)

# ── Session directory ─────────────────────────────────────────────────────────

def sessions_dir() -> Path:
    d = Path.home() / ".agent-forge" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _index_path() -> Path:
    return sessions_dir() / "index.json"

def _read_index() -> dict[str, str]:
    """Read {cwd: latest_session_id}. Returns {} on missing/corrupt index."""
    p = _index_path()
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _write_index(idx: dict[str, str]) -> None:
    """Atomic-ish write: tmp file + os.replace."""
    p = _index_path()
    tmp = p.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        os.replace(tmp, p)
    except Exception:
        # Best-effort — index corruption falls back to O(n) scan in latest_session_id
        pass

def memory_path(scope: str, cwd: str) -> Path:
    if scope == "global":
        p = Path.home() / ".agent-forge" / "memory.md"
    else:
        p = Path(cwd) / ".agent-forge" / "memory.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

# ── ID generation ─────────────────────────────────────────────────────────────

def new_id() -> str:
    return str(uuid.uuid4()).replace("-", "")[:16]

# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _session_path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.jsonl"

def append_entry(session_id: str, entry: dict) -> None:
    with open(_session_path(session_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def append_metadata(session_id: str, model_id: str, cwd: str) -> None:
    append_entry(session_id, {
        "type": "metadata", "id": new_id(),
        "ts": int(time.time() * 1000), "model": model_id, "cwd": cwd,
    })
    # Update the cwd → session index for O(1) latest_session_id lookup.
    try:
        idx = _read_index()
        idx[cwd] = session_id
        _write_index(idx)
    except Exception:
        pass

def append_message(session_id: str, msg: Message, usage: TokenUsage | None = None) -> str:
    msg_id = new_id()
    entry: dict = {
        "type": "message", "id": msg_id,
        "ts": int(time.time() * 1000),
        "message": _msg_to_dict(msg),
    }
    if usage:
        entry["usage"] = {
            "input": usage.input, "output": usage.output,
            "cache_read": usage.cache_read, "cache_write": usage.cache_write,
            "cost": usage.cost,
        }
    append_entry(session_id, entry)
    return msg_id

def append_compaction(session_id: str, summary: str, first_kept_id: str,
                      tokens_before: int, tokens_after: int) -> None:
    append_entry(session_id, {
        "type": "compaction", "id": new_id(),
        "ts": int(time.time() * 1000),
        "summary": summary,
        "first_kept_id": first_kept_id,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
    })

# ── Session resume ────────────────────────────────────────────────────────────

@dataclass
class ResumedSession:
    session_id: str
    messages: list[Message]
    last_usage: TokenUsage

def list_sessions() -> list[tuple[str, float]]:
    """Return (session_id, mtime) sorted newest-first."""
    result = []
    for p in sessions_dir().glob("*.jsonl"):
        result.append((p.stem, p.stat().st_mtime))
    result.sort(key=lambda x: x[1], reverse=True)
    return result

def latest_session_id(cwd: str) -> str | None:
    """Find the most recent session that was started in cwd.

    Fast path: read ~/.agent-forge/sessions/index.json (maintained by
    append_metadata). Falls back to an O(n) scan if the index is missing,
    corrupt, or stale (the indexed session no longer exists on disk).
    """
    idx = _read_index()
    sid = idx.get(cwd)
    if sid and _session_path(sid).exists():
        return sid
    # Slow fallback — also rebuild the index entry while we're at it.
    for sid, _ in list_sessions():
        try:
            with open(_session_path(sid), encoding="utf-8") as f:
                first_line = f.readline()
            entry = json.loads(first_line)
            if entry.get("type") == "metadata" and entry.get("cwd") == cwd:
                try:
                    idx[cwd] = sid
                    _write_index(idx)
                except Exception:
                    pass
                return sid
        except Exception:
            continue
    return None

def resume_session(session_id: str) -> ResumedSession:
    path = _session_path(session_id)
    if not path.exists():
        return ResumedSession(session_id=session_id, messages=[], last_usage=ZERO_USAGE)

    entries: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # Find last compaction entry — only load messages after it
    last_compaction_first_kept: str | None = None
    for e in entries:
        if e.get("type") == "compaction":
            last_compaction_first_kept = e.get("first_kept_id")

    start_after_id = last_compaction_first_kept

    messages: list[Message] = []
    last_usage = ZERO_USAGE
    found_start = (start_after_id is None)

    for entry in entries:
        if entry.get("type") != "message":
            continue
        if not found_start:
            if entry.get("id") == start_after_id:
                found_start = True
            else:
                continue
        try:
            msg = _dict_to_msg(entry["message"])
            # Re-stitch outer-entry usage onto the AssistantMessage so the
            # round-trip is information-preserving (cost/token accounting on
            # resume matches what was originally written).
            if "usage" in entry:
                u = entry["usage"]
                usage = TokenUsage(
                    input=u.get("input", 0), output=u.get("output", 0),
                    cache_read=u.get("cache_read", 0), cache_write=u.get("cache_write", 0),
                    cost=u.get("cost", 0.0),
                )
                last_usage = usage
                if isinstance(msg, AssistantMessage) and msg.usage is None:
                    # Frozen dataclass — reconstruct rather than mutate.
                    msg = AssistantMessage(
                        content=msg.content,
                        stop_reason=msg.stop_reason,
                        usage=usage,
                        model_id=msg.model_id,
                        timestamp=msg.timestamp,
                    )
            messages.append(msg)
        except Exception:
            continue

    return ResumedSession(session_id=session_id, messages=messages, last_usage=last_usage)

# ── Message serialisation ─────────────────────────────────────────────────────

def _msg_to_dict(msg: Message) -> dict:
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            content: str | list = msg.content
        else:
            content = []
            for blk in msg.content:
                if isinstance(blk, TextContent):
                    content.append({"type": "text", "text": blk.text})
                elif isinstance(blk, ImageContent):
                    content.append({"type": "image", "media_type": blk.media_type, "data": blk.data})
        return {"role": "user", "content": content, "ts": msg.timestamp}
    elif isinstance(msg, AssistantMessage):
        content = []
        for blk in msg.content:
            if isinstance(blk, TextContent):
                content.append({"type": "text", "text": blk.text})
            elif isinstance(blk, ThinkingContent):
                content.append({"type": "thinking", "thinking": blk.thinking, "signature": blk.signature})
            elif isinstance(blk, ToolCallContent):
                content.append({"type": "tool_use", "id": blk.id, "name": blk.name, "arguments": blk.arguments})
        d: dict = {"role": "assistant", "content": content, "ts": msg.timestamp,
                   "stop_reason": msg.stop_reason}
        if msg.model_id is not None:
            d["model_id"] = msg.model_id
        return d
    else:  # ToolResultMessage
        if isinstance(msg.content, str):
            serialized_content: str | list = msg.content
        else:
            serialized_content = []
            for blk in msg.content:
                if isinstance(blk, TextContent):
                    serialized_content.append({"type": "text", "text": blk.text})
                elif isinstance(blk, ImageContent):
                    serialized_content.append({"type": "image", "media_type": blk.media_type, "data": blk.data})
        return {"role": "tool_result", "tool_call_id": msg.tool_call_id,
                "content": serialized_content, "is_error": msg.is_error, "ts": msg.timestamp}


def _dict_to_msg(d: dict) -> Message:
    role = d.get("role", "")
    ts = d.get("ts", int(time.time() * 1000))
    if role == "user":
        raw = d["content"]
        if isinstance(raw, str):
            return UserMessage(content=raw, timestamp=ts)
        blocks: list[TextContent | ImageContent] = []
        for c in raw:
            t = c.get("type")
            if t == "text":
                blocks.append(TextContent(text=c["text"]))
            elif t == "image":
                blocks.append(ImageContent(media_type=c["media_type"], data=c["data"]))
        return UserMessage(content=tuple(blocks) if blocks else "", timestamp=ts)
    elif role == "assistant":
        a_blocks: list = []
        for blk in d.get("content", []):
            t = blk.get("type")
            if t == "text":
                a_blocks.append(TextContent(text=blk["text"]))
            elif t == "thinking":
                a_blocks.append(ThinkingContent(thinking=blk["thinking"], signature=blk.get("signature")))
            elif t == "tool_use":
                a_blocks.append(ToolCallContent(id=blk["id"], name=blk["name"], arguments=blk.get("arguments", {})))
        # Inner-dict 'usage' (rare; some legacy entries write it here) takes
        # priority because the outer-entry 'usage' is reconstructed by the
        # caller via _entry_to_msg() which knows about the full JSONL line.
        u = d.get("usage")
        usage = TokenUsage(
            input=u.get("input", 0), output=u.get("output", 0),
            cache_read=u.get("cache_read", 0), cache_write=u.get("cache_write", 0),
            cost=u.get("cost", 0.0),
        ) if u else None
        return AssistantMessage(
            content=tuple(a_blocks),
            stop_reason=d.get("stop_reason", "end_turn"),
            usage=usage,
            model_id=d.get("model_id"),
            timestamp=ts,
        )
    else:  # tool_result
        raw_content = d.get("content", "")
        if isinstance(raw_content, str):
            tr_content: str | tuple = raw_content
        else:
            parts: list[TextContent | ImageContent] = []
            for blk in raw_content:
                if blk.get("type") == "text":
                    parts.append(TextContent(text=blk["text"]))
                elif blk.get("type") == "image":
                    parts.append(ImageContent(media_type=blk["media_type"], data=blk["data"]))
            tr_content = tuple(parts) if parts else ""
        return ToolResultMessage(
            tool_call_id=d["tool_call_id"], content=tr_content,
            is_error=d.get("is_error", False), timestamp=ts,
        )

# ── Memory ────────────────────────────────────────────────────────────────────

_MEMORY_CAP_TOKENS = 2000
_DEDUP_PREFIX = 60

def load_memory(cwd: str) -> str:
    """Load and merge global + project memory.md."""
    parts: list[str] = []
    for scope in ("global", "project"):
        p = memory_path(scope, cwd)
        if p.exists():
            parts.append(p.read_text(encoding="utf-8").strip())
    return "\n".join(p for p in parts if p)

def load_memory_deduped(cwd: str, context_contents: list[str] | None = None) -> str:
    """Load memory, filtering entries already present in context files."""
    raw = load_memory(cwd)
    if not raw:
        return ""
    context_text = "\n".join(context_contents or [])
    lines = raw.splitlines()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            result.append(line)
            continue
        prefix = stripped[:_DEDUP_PREFIX].lower()
        if prefix not in context_text.lower():
            result.append(line)
    return "\n".join(result)

def update_memory(cwd: str, learnings: list[str], scope: str = "project") -> None:
    """Append learnings to memory, deduplicating and capping at ~2K tokens."""
    p = memory_path(scope, cwd)
    existing = p.read_text(encoding="utf-8") if p.exists() else "## Memory\n"

    today = __import__("datetime").date.today().isoformat()
    new_lines: list[str] = []
    for learning in learnings:
        entry = f"- {learning.strip()} (learned {today})"
        prefix = entry[:_DEDUP_PREFIX].lower()
        if prefix not in existing.lower():
            new_lines.append(entry)

    if not new_lines:
        return

    updated = existing.rstrip() + "\n" + "\n".join(new_lines) + "\n"

    # Enforce token cap (keep most recent entries)
    while len(updated.encode()) // 4 > _MEMORY_CAP_TOKENS:
        lines = updated.splitlines()
        bullet_lines = [i for i, l in enumerate(lines) if l.strip().startswith("-")]
        if not bullet_lines:
            break
        lines.pop(bullet_lines[0])
        updated = "\n".join(lines) + "\n"

    p.write_text(updated, encoding="utf-8")
