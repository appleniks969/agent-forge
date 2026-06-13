"""JsonlStore: fsync'd append-only JSONL EventStore with a sidecar index.

Layer: adapters — imports ports + kernel only. One file per session
(`{sid}.jsonl`) under an explicit root; durable envelopes are redacted
(rewrite-before-append, so disk and every fold over it agree), serialized
via the kernel envelope serde, and fsync'd per append. Transient envelopes
are bus-only and never touch disk. A sidecar `index.v2.json` maps
sid -> {path, last_seq, updated_at} for O(1) latest-session lookup; it is a
cache — the logs are truth and the index is rebuildable by scan. The name is
versioned because the legacy CLI keeps an incompatible `{cwd: sid}` index.json
in the same spec-mandated sessions directory; sharing the filename would make
each program invalidate the other's cache on every write.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

from forge.kernel.events import (
    Envelope,
    Event,
    envelope_from_dict,
    envelope_to_dict,
)

Redactor = Callable[[Event], Event]

INDEX_NAME = "index.v2.json"


def default_root() -> Path:
    return Path.home() / ".forge" / "sessions"


class JsonlStore:
    """Append-only EventStore over one JSONL file per session."""

    def __init__(
        self,
        root: Path,
        sid: str,
        *,
        redactor: Redactor | None = None,
        cwd: str | None = None,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._sid = sid
        self._path = self._root / f"{sid}.jsonl"
        self._redactor = redactor
        self._cwd = cwd  # recorded in the index so `--continue` is per-project
        self._fh: TextIO | None = None
        self._next_seq = self._recover()

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def path(self) -> Path:
        return self._path

    def append(self, env: Envelope) -> None:
        if not env.durable:
            return  # transients are bus-only by contract; never persisted
        if self._redactor is not None:
            # Rewrite-before-append: only the redacted body is ever serialized.
            env = replace(env, body=self._redactor(env.body))
        line = json.dumps(envelope_to_dict(env), ensure_ascii=False)
        fh = self._ensure_open()
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        self._next_seq = max(self._next_seq, env.seq + 1)
        self._update_index(env.seq)

    def replay(self) -> list[Envelope]:
        return _read_log(self._path)

    def next_seq(self) -> int:
        return self._next_seq

    async def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # -- internals ------------------------------------------------------------

    def _ensure_open(self) -> TextIO:
        if self._fh is None or self._fh.closed:
            self._fh = open(self._path, "a", encoding="utf-8")
        return self._fh

    def _recover(self) -> int:
        """Scan the existing log for next_seq; truncate a trailing torn write.

        Truncation is required, not cosmetic: appending after a partial line
        would concatenate the next record onto the torn tail and corrupt it.
        """
        if not self._path.exists():
            return 0
        with open(self._path, "rb") as f:
            lines = f.readlines()
        next_seq = 0
        good_end = 0
        for i, raw in enumerate(lines):
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if i == len(lines) - 1:
                    os.truncate(self._path, good_end)
                    break
                raise ValueError(f"corrupt log line {i + 1} in {self._path}") from None
            env = _decode_line(d, i, self._path)
            next_seq = env.seq + 1
            good_end += len(raw)
        return next_seq

    def _update_index(self, last_seq: int) -> None:
        # Best-effort: an index write failure must never fail the append —
        # the log is already fsync'd and the index is rebuildable by scan.
        try:
            index = _read_index(self._root)
            if index is None:
                index = _scan_logs(self._root)
            entry: dict[str, Any] = {
                "path": str(self._path),
                "last_seq": last_seq,
                "updated_at": time.time(),
            }
            # Preserve a known cwd across appends that don't carry one.
            cwd = self._cwd or index.get(self._sid, {}).get("cwd")
            if cwd is not None:
                entry["cwd"] = cwd
            index[self._sid] = entry
            _write_index(self._root, index)
        except OSError:
            pass


# -- sidecar index --------------------------------------------------------------


def load_index(root: Path) -> dict[str, dict[str, Any]]:
    """Read the sidecar index, rebuilding by scan if missing or corrupt.

    A not-yet-created sessions root has no sessions and is read-only here
    (`forge sessions` before the first run must not try to create/write it)."""
    if not Path(root).is_dir():
        return {}
    index = _read_index(Path(root))
    if index is None:
        index = rebuild_index(root)
    return index


def rebuild_index(root: Path) -> dict[str, dict[str, Any]]:
    root = Path(root)
    index = _scan_logs(root)
    _write_index(root, index)
    return index


def latest_sid(root: Path, *, cwd: str | None = None) -> str | None:
    """Most recently updated session sid, optionally restricted to one cwd."""
    index = load_index(root)
    items = [
        (sid, e)
        for sid, e in index.items()
        if cwd is None or e.get("cwd") == cwd
    ]
    if not items:
        return None
    return max(items, key=lambda kv: kv[1]["updated_at"])[0]


def session_summaries(
    root: Path, *, cwd: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent sessions newest-first: sid, cwd, updated_at, and the first prompt
    (read from each log's first UserSubmitted event). For `forge sessions`."""
    index = load_index(root)
    rows = [
        (sid, e)
        for sid, e in index.items()
        if cwd is None or e.get("cwd") == cwd
    ]
    rows.sort(key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
    out: list[dict[str, Any]] = []
    for sid, e in rows[:limit]:
        out.append(
            {
                "sid": sid,
                "cwd": e.get("cwd"),
                "updated_at": e.get("updated_at", 0.0),
                "prompt": _first_prompt(Path(e.get("path", root / f"{sid}.jsonl"))),
            }
        )
    return out


def _first_prompt(path: Path) -> str:
    """The first UserSubmitted text in a log, for session listings; '' if none."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == "UserSubmitted":
                    return str(rec.get("body", {}).get("text", ""))[:80]
    except OSError:
        return ""
    return ""


def _read_index(root: Path) -> dict[str, dict[str, Any]] | None:
    try:
        data = json.loads((root / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or not all(
        isinstance(v, dict) and {"path", "last_seq", "updated_at"} <= v.keys()
        for v in data.values()
    ):
        return None
    return data


def _write_index(root: Path, index: dict[str, dict[str, Any]]) -> None:
    # Atomic replace so a crash mid-write leaves the old index, never half a file.
    tmp = root / (INDEX_NAME + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(root / INDEX_NAME)


def _scan_logs(root: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*.jsonl")):
        try:
            envs = _read_log(path)
        except ValueError:
            continue  # a corrupt foreign log must not block recovering the rest
        if not envs:
            continue
        index[path.stem] = {
            "path": str(path),
            "last_seq": envs[-1].seq,
            "updated_at": path.stat().st_mtime,
        }
    return index


# -- log reading -----------------------------------------------------------------


def _read_log(path: Path) -> list[Envelope]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        lines = f.readlines()
    envs: list[Envelope] = []
    for i, raw in enumerate(lines):
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if i == len(lines) - 1:
                break  # trailing torn write: the valid prefix is the log
            raise ValueError(f"corrupt log line {i + 1} in {path}") from None
        envs.append(_decode_line(d, i, path))
    return envs


def _decode_line(d: Any, i: int, path: Path) -> Envelope:
    # Foreign-schema lines (e.g. legacy v1 session logs sharing the sessions
    # dir) must surface as ValueError: replay's documented corruption signal,
    # and the type _scan_logs already skips. Torn writes never reach here —
    # a truncated line fails JSON decoding, never schema decoding.
    try:
        return envelope_from_dict(d)
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"corrupt log line {i + 1} in {path}") from None
