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

    def __init__(self, root: Path, sid: str, *, redactor: Redactor | None = None) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._sid = sid
        self._path = self._root / f"{sid}.jsonl"
        self._redactor = redactor
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
            index[self._sid] = {
                "path": str(self._path),
                "last_seq": last_seq,
                "updated_at": time.time(),
            }
            _write_index(self._root, index)
        except OSError:
            pass


# -- sidecar index --------------------------------------------------------------


def load_index(root: Path) -> dict[str, dict[str, Any]]:
    """Read the sidecar index, rebuilding by scan if missing or corrupt."""
    index = _read_index(Path(root))
    if index is None:
        index = rebuild_index(root)
    return index


def rebuild_index(root: Path) -> dict[str, dict[str, Any]]:
    root = Path(root)
    index = _scan_logs(root)
    _write_index(root, index)
    return index


def latest_sid(root: Path) -> str | None:
    index = load_index(root)
    if not index:
        return None
    return max(index.items(), key=lambda kv: kv[1]["updated_at"])[0]


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
