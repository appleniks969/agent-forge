"""Unit tests for the shared noise classifier (`wiki/_noise.py`)."""
from __future__ import annotations

import pytest

from agent_forge.wiki._noise import is_noisy_path


@pytest.mark.parametrize("path", [
    # Lockfiles + changelogs (suffix matches)
    "package-lock.json",
    "frontend/package-lock.json",
    "yarn.lock",
    "uv.lock",
    "Cargo.lock",
    "poetry.lock",
    "Gemfile.lock",
    "go.sum",
    "Pipfile.lock",
    "composer.lock",
    "packages/foo/CHANGELOG.md",
    "packages/foo/changelog.md",     # case variant
    # Generated files (infix)
    "packages/ai/src/models.generated.ts",
    "src/codegen/foo.gen.py",
    "src/some/generated/api.py",
    # Build / dep / VCS infrastructure (prefix at repo root)
    ".github/workflows/ci.yml",
    ".github/APPROVED_CONTRIBUTORS",
    "node_modules/foo/index.js",
    "dist/bundle.js",
    "build/output.txt",
    ".next/cache/foo",
    "__pycache__/foo.pyc",
    ".pytest_cache/v/cache/lastfailed",
    ".gradle/8.13/checksums/checksums.lock",
    "target/release/myapp",
    # Same dirs nested (infix)
    "packages/foo/node_modules/bar.js",
    "frontend/dist/main.js",
    "subdir/__pycache__/cache.pyc",
])
def test_is_noisy_path_classifies_known_noise(path: str) -> None:
    assert is_noisy_path(path), f"expected {path!r} to be classified as noise"


@pytest.mark.parametrize("path", [
    # Real source — must NOT be noise
    "src/agent.py",
    "packages/coding-agent/src/modes/interactive/interactive-mode.ts",
    "packages/ai/src/providers/openai-completions.ts",
    "agent_forge/wiki/present/runner.py",
    # Tests are real signal
    "tests/wiki/test_present.py",
    "packages/agent/test/agent-loop.test.ts",
    # Documentation files (other than CHANGELOG)
    "docs/extensions.md",
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    # Files containing "bot" in the name shouldn't trigger anything (we filter
    # bots in authorship not paths)
    "src/bot.py",
    "src/chatbot/handlers.ts",
])
def test_is_noisy_path_keeps_real_paths(path: str) -> None:
    assert not is_noisy_path(path), f"expected {path!r} to NOT be classified as noise"


def test_is_noisy_path_handles_empty_input() -> None:
    """Empty / falsy input → False. We don't pretend missing paths are noise."""
    assert is_noisy_path("") is False
    assert is_noisy_path(None) is False  # type: ignore[arg-type]
