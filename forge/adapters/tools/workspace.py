"""Root-anchored Workspace implementation: the only path authority.

Layer: adapters/tools — implements the forge.ports.tool.Workspace protocol.
Every path a tool touches goes through resolve(), which anchors relative
paths at the root, normalizes ``..``, follows symlinks, and raises
WorkspaceEscape for anything that lands outside the root.
"""

from __future__ import annotations

from pathlib import Path

from forge.ports.tool import WorkspaceEscape


class RootedWorkspace:
    """Workspace anchored at a resolved root directory."""

    def __init__(self, root: str | Path) -> None:
        # Resolve once so containment compares like with like even when the
        # root itself is reached through a symlink (e.g. macOS /tmp).
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, path: str) -> Path:
        raw = Path(path)
        # Path.joinpath with an absolute argument would silently discard the
        # root (the legacy os.path.join escape); treat absolutes explicitly.
        candidate = raw if raw.is_absolute() else self._root / raw
        try:
            resolved = candidate.resolve()
        except OSError as exc:  # symlink loops and unresolvable paths
            raise WorkspaceEscape(f"cannot resolve {path!r}: {exc}") from None
        if not resolved.is_relative_to(self._root):
            raise WorkspaceEscape(f"{path!r} escapes the workspace root")
        return resolved
