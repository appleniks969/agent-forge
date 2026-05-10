"""
agent_forge.wiki._noise — shared "is this path noise?" classifier.

Used by `present.runner` (filters hot files in the system-prompt section) AND
`compile.bundle` (filters hot files before they reach the LLM). Single source
of truth so both surfaces agree on what counts as engineering signal vs.
mechanical churn.

Three failure modes we filter:

  * Auto-generated files — `*.generated.*`, `/generated/`, `*.gen.*`
  * Lockfiles + changelogs — always edited by tooling
  * Build / dependency / VCS infrastructure — `.github/`, `node_modules/`,
    `dist/`, `build/`, `.next/`, `__pycache__/`, …

These are leaf-rules — they don't filter "things the user might still want."
A commit that touches *only* a CHANGELOG is mechanical; a commit that touches
both source and CHANGELOG is real work. We classify per-path here; callers
decide how to use the result.
"""
from __future__ import annotations


_NOISE_SUFFIXES = (
    "/CHANGELOG.md",
    "/changelog.md",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "uv.lock",
    "poetry.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
    "Pipfile.lock",
)

_NOISE_INFIXES = (
    ".generated.",
    ".gen.",
    "/generated/",
    "/.git/",
    "/.github/",
    "/node_modules/",
    "/dist/",
    "/build/",
    "/.next/",
    "/.nuxt/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.mypy_cache/",
    "/.gradle/",
    "/target/",
)

# Same patterns as infixes, but matching at the start of a repo-relative path
# (the common case where the noise dir sits at the repo root).
_NOISE_PREFIXES = (
    ".git/",
    ".github/",
    "node_modules/",
    "dist/",
    "build/",
    ".next/",
    ".nuxt/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".gradle/",
    "target/",
)


def is_noisy_path(path: str) -> bool:
    """Return True if ``path`` is auto-edited / generated and should not be
    surfaced as engineering signal.

    Empty / falsy input returns False (callers should handle missing paths
    upstream — we don't pretend an empty string is noise).
    """
    if not path:
        return False
    if any(path.endswith(suf) for suf in _NOISE_SUFFIXES):
        return True
    if any(inf in path for inf in _NOISE_INFIXES):
        return True
    if any(path.startswith(pre) for pre in _NOISE_PREFIXES):
        return True
    return False
