"""Make `wiki.*` importable for tests.

Adds the sibling `scripts/` directory to ``sys.path`` so test imports of the
form ``from wiki.X import Y`` resolve to the moved skill code at
``../scripts/wiki/``.

The skill is intentionally self-contained for its own logic, but still
soft-depends on the installed ``agent_forge`` package for ``messages``,
``models``, and ``provider`` (which provide OAuth dispatch, retry, and JSON
repair). That dependency is satisfied by running tests inside the
agent-forge repo with ``uv run pytest``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = (Path(__file__).parent.parent / "scripts").resolve()
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
