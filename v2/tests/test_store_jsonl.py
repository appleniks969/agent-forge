"""JsonlStore: roundtrip, fsync'd JSONL, torn writes, redaction, index, fold parity."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from forge.adapters.jsonl_store import (
    INDEX_NAME,
    JsonlStore,
    default_root,
    latest_sid,
    load_index,
    rebuild_index,
)
from forge.kernel.events import (
    DURABLE_KINDS,
    AssistantBlock,
    ChildSpawned,
    Compacted,
    Envelope,
    Event,
    PermissionAsked,
    PermissionDecided,
    RetryScheduled,
    SessionEnded,
    TextDelta,
    ThinkingDelta,
    ToolDeclared,
    ToolFinished,
    ToolOutputChunk,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UserSubmitted,
    kind_of,
    make_envelope,
)
from forge.kernel.state import fold
from forge.kernel.types import (
    PermissionQuestion,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    Usage,
)
from forge.ports.store import EventStore

CALL = ToolCall(id="c1", name="read", args={"path": "x.py"})


def durable_events() -> list[Event]:
    return [
        UserSubmitted("fix the bug"),
        TurnStarted(1),
        AssistantBlock(TextBlock("looking at it")),
        AssistantBlock(ThinkingBlock("hmm")),
        AssistantBlock(CALL),
        ToolDeclared(CALL),
        ToolStarted("c1"),
        ToolFinished(ToolResult("c1", "file contents", is_error=False)),
        PermissionAsked(PermissionQuestion("c1", "read", "allow read?")),
        PermissionDecided("c1", True, "user", "approved at prompt"),
        Compacted("earlier work summarized", 0),
        RetryScheduled(2, 1.5, "overloaded_error"),
        TurnFinished("ok", Usage(10, 5, 2, 1), 0.0123),
        ChildSpawned("child-1"),
        SessionEnded(),
    ]


def envelopes_for(events: list[Event], sid: str = "s1") -> list[Envelope]:
    return [make_envelope(seq=i, sid=sid, body=e, ts=float(i)) for i, e in enumerate(events)]


def append_all(store: JsonlStore, envs: list[Envelope]) -> None:
    for env in envs:
        store.append(env)


# -- roundtrip + file format -----------------------------------------------------


def test_roundtrip_every_durable_kind(tmp_path: Path) -> None:
    events = durable_events()
    assert {kind_of(e) for e in events} == DURABLE_KINDS, "test must cover every durable kind"
    envs = envelopes_for(events)
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    assert store.replay() == envs


def test_file_is_valid_jsonl(tmp_path: Path) -> None:
    envs = envelopes_for(durable_events())
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(envs)
    for line, env in zip(lines, envs):
        d = json.loads(line)
        assert d["seq"] == env.seq
        assert d["sid"] == "s1"
        assert d["durable"] is True
        assert d["kind"] == kind_of(env.body)


def test_transient_envelopes_never_persisted(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path, "s1")
    durable = make_envelope(seq=0, sid="s1", body=UserSubmitted("hi"))
    store.append(durable)
    seq_before = store.next_seq()
    for i, body in enumerate(
        [TextDelta("par"), ThinkingDelta("tial"), ToolOutputChunk("c1", "chunk")], start=1
    ):
        store.append(make_envelope(seq=i, sid="s1", body=body))
    assert store.next_seq() == seq_before
    assert store.replay() == [durable]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 1


# -- torn writes -------------------------------------------------------------------


def test_replay_tolerates_torn_trailing_partial_line(tmp_path: Path) -> None:
    envs = envelopes_for(durable_events()[:3])
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    with open(store.path, "ab") as f:
        f.write(b'{"seq": 99, "sid": "s1", "par')  # crash mid-append, no newline
    assert store.replay() == envs


def test_replay_tolerates_truncated_last_line(tmp_path: Path) -> None:
    envs = envelopes_for(durable_events()[:3])
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    data = store.path.read_bytes()
    store.path.write_bytes(data[:-15])  # tear the last complete record
    assert store.replay() == envs[:-1]


def test_reopen_truncates_torn_tail_and_resumes_seq(tmp_path: Path) -> None:
    envs = envelopes_for(durable_events()[:3])
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    with open(store.path, "ab") as f:
        f.write(b'{"torn": ')
    reopened = JsonlStore(tmp_path, "s1")
    assert reopened.next_seq() == 3
    nxt = make_envelope(seq=3, sid="s1", body=SessionEnded(), ts=3.0)
    reopened.append(nxt)
    # The torn tail must not corrupt the next record.
    assert reopened.replay() == envs + [nxt]
    assert JsonlStore(tmp_path, "s1").next_seq() == 4


# -- redaction ---------------------------------------------------------------------


def test_redactor_applied_before_disk(tmp_path: Path) -> None:
    secret = "sk-secret-12345"

    def redact(event: Event) -> Event:
        if isinstance(event, ToolFinished) and secret in event.result.content:
            masked = event.result.content.replace(secret, "[redacted]")
            return ToolFinished(ToolResult(event.result.call_id, masked, event.result.is_error))
        return event

    store = JsonlStore(tmp_path, "s1", redactor=redact)
    store.append(
        make_envelope(seq=0, sid="s1", body=ToolFinished(ToolResult("c1", f"key={secret}")))
    )
    raw = store.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "[redacted]" in raw
    replayed = store.replay()
    assert isinstance(replayed[0].body, ToolFinished)
    assert replayed[0].body.result.content == "key=[redacted]"


# -- sidecar index -------------------------------------------------------------------


def test_index_updated_on_append(tmp_path: Path) -> None:
    envs = envelopes_for(durable_events()[:4])
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    index = json.loads((tmp_path / INDEX_NAME).read_text(encoding="utf-8"))
    entry = index["s1"]
    assert entry["path"] == str(store.path)
    assert entry["last_seq"] == 3
    assert entry["updated_at"] > 0


def test_latest_sid_tracks_most_recent_append(tmp_path: Path) -> None:
    a = JsonlStore(tmp_path, "aaa")
    b = JsonlStore(tmp_path, "bbb")
    a.append(make_envelope(seq=0, sid="aaa", body=UserSubmitted("first")))
    b.append(make_envelope(seq=0, sid="bbb", body=UserSubmitted("second")))
    assert latest_sid(tmp_path) == "bbb"
    a.append(make_envelope(seq=1, sid="aaa", body=SessionEnded()))
    assert latest_sid(tmp_path) == "aaa"


def test_index_rebuilt_when_missing(tmp_path: Path) -> None:
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envelopes_for(durable_events()[:3]))
    (tmp_path / INDEX_NAME).unlink()
    index = load_index(tmp_path)
    assert index["s1"]["last_seq"] == 2
    assert index["s1"]["path"] == str(store.path)
    assert (tmp_path / INDEX_NAME).exists()


def test_index_rebuilt_when_corrupt(tmp_path: Path) -> None:
    a = JsonlStore(tmp_path, "aaa")
    a.append(make_envelope(seq=0, sid="aaa", body=UserSubmitted("hi")))
    time.sleep(0.01)  # distinct mtimes for the rebuild's updated_at ordering
    b = JsonlStore(tmp_path, "bbb")
    b.append(make_envelope(seq=0, sid="bbb", body=UserSubmitted("yo")))
    (tmp_path / INDEX_NAME).write_text("not json{{{", encoding="utf-8")
    assert latest_sid(tmp_path) == "bbb"
    index = rebuild_index(tmp_path)
    assert set(index) == {"aaa", "bbb"}


def test_corrupt_index_self_heals_on_next_append(tmp_path: Path) -> None:
    a = JsonlStore(tmp_path, "aaa")
    a.append(make_envelope(seq=0, sid="aaa", body=UserSubmitted("hi")))
    (tmp_path / INDEX_NAME).write_text("garbage", encoding="utf-8")
    b = JsonlStore(tmp_path, "bbb")
    b.append(make_envelope(seq=0, sid="bbb", body=UserSubmitted("yo")))
    index = json.loads((tmp_path / INDEX_NAME).read_text(encoding="utf-8"))
    assert set(index) == {"aaa", "bbb"}  # rebuilt by scan, not just overwritten


# -- foreign logs (legacy v1 schema shares the sessions dir) --------------------------


LEGACY_LINE = (
    '{"type": "metadata", "id": "151f9c72c68f4081", "ts": 1777299172748,'
    ' "model": "claude-sonnet-4-6", "cwd": "/tmp/elsewhere"}\n'
)


def test_replay_of_foreign_schema_log_raises_value_error(tmp_path: Path) -> None:
    # Valid JSON, wrong schema (no "kind"): corruption, not a torn write.
    store = JsonlStore(tmp_path, "s1")
    store.append(make_envelope(seq=0, sid="s1", body=UserSubmitted("hi")))
    store.path.write_text(LEGACY_LINE * 2, encoding="utf-8")
    with pytest.raises(ValueError):
        store.replay()


def test_open_on_foreign_schema_log_raises_value_error(tmp_path: Path) -> None:
    (tmp_path / "s1.jsonl").write_text(LEGACY_LINE * 2, encoding="utf-8")
    with pytest.raises(ValueError):
        JsonlStore(tmp_path, "s1")


def test_scan_skips_foreign_schema_logs(tmp_path: Path) -> None:
    (tmp_path / "legacy.jsonl").write_text(LEGACY_LINE, encoding="utf-8")
    store = JsonlStore(tmp_path, "s1")
    store.append(make_envelope(seq=0, sid="s1", body=UserSubmitted("hi")))
    index = rebuild_index(tmp_path)
    assert set(index) == {"s1"}


def test_append_with_foreign_logs_and_legacy_index_present(tmp_path: Path) -> None:
    # The legacy CLI keeps a {cwd: sid} index.json in the same directory;
    # v2 must neither crash on it nor overwrite it (versioned sidecar name).
    (tmp_path / "legacy.jsonl").write_text(LEGACY_LINE, encoding="utf-8")
    legacy_index = '{"/tmp/elsewhere": "legacy"}'
    (tmp_path / "index.json").write_text(legacy_index, encoding="utf-8")
    store = JsonlStore(tmp_path, "s1")
    store.append(make_envelope(seq=0, sid="s1", body=UserSubmitted("hi")))
    assert INDEX_NAME != "index.json"
    assert (tmp_path / "index.json").read_text(encoding="utf-8") == legacy_index
    assert load_index(tmp_path)["s1"]["last_seq"] == 0


# -- fold parity ----------------------------------------------------------------------


def test_fold_of_replay_equals_fold_of_originals(tmp_path: Path) -> None:
    events: list[Event] = [
        UserSubmitted("read x.py"),
        TurnStarted(1),
        TextDelta("rea"),  # transient: skipped by both store and fold
        AssistantBlock(TextBlock("reading")),
        AssistantBlock(CALL),
        ToolDeclared(CALL),
        ToolStarted("c1"),
        ToolFinished(ToolResult("c1", "contents")),
        AssistantBlock(TextBlock("done")),
        TurnFinished("ok", Usage(20, 10), None),
        SessionEnded(),
    ]
    envs = envelopes_for(events)
    store = JsonlStore(tmp_path, "s1")
    append_all(store, envs)
    state = fold(store.replay())
    assert state == fold(envs)
    assert state.finished
    assert len(state.messages) == 4  # user, assistant(text+call), results, assistant(done)


# -- port conformance + lifecycle ------------------------------------------------------


async def test_satisfies_event_store_port_and_close(tmp_path: Path) -> None:
    store: EventStore = JsonlStore(tmp_path, "s1")
    assert store.next_seq() == 0
    store.append(make_envelope(seq=0, sid="s1", body=UserSubmitted("hi")))
    assert store.next_seq() == 1
    await store.close()
    await store.close()  # idempotent
    assert JsonlStore(tmp_path, "s1").replay() == store.replay()


def test_default_root_points_home(tmp_path: Path) -> None:
    assert default_root() == Path.home() / ".forge" / "sessions"
    assert latest_sid(tmp_path) is None  # empty root: no sessions, no crash


# --- per-cwd session listing (for `forge --continue` / `forge sessions`) ---------

def test_latest_sid_and_summaries_filter_by_cwd(tmp_path):
    from forge.adapters.jsonl_store import JsonlStore, latest_sid, session_summaries
    from forge.kernel.events import UserSubmitted, make_envelope

    def write(sid, cwd, text):
        s = JsonlStore(tmp_path, sid, cwd=cwd)
        s.append(make_envelope(0, sid, body=UserSubmitted(text)))
        return s

    write("a", "/proj/x", "alpha")
    write("b", "/proj/y", "beta")
    write("c", "/proj/x", "gamma")

    # latest_sid scoped to a cwd ignores other directories' sessions.
    assert latest_sid(tmp_path, cwd="/proj/x") == "c"
    assert latest_sid(tmp_path, cwd="/proj/y") == "b"
    assert latest_sid(tmp_path) in {"a", "b", "c"}  # global: most recent

    rows = session_summaries(tmp_path, cwd="/proj/x")
    assert [r["sid"] for r in rows] == ["c", "a"]  # newest first, x only
    assert rows[0]["prompt"] == "gamma"  # first-prompt preview from the log
