#!/usr/bin/env bash
# build_api_docs.sh — generate the HTML API reference under docs/api/.
#
# The output is auto-generated and not checked in (see .gitignore).
# Run after any docstring change:
#
#     bash scripts/build_api_docs.sh
#     open docs/api/agent_forge.html       # macOS
#     xdg-open docs/api/agent_forge.html   # linux
#
# CI can serve the same output statically.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/docs/api"

cd "$REPO_ROOT"

if ! command -v pdoc >/dev/null 2>&1; then
  echo "pdoc not found; installing dev deps via uv..." >&2
  uv sync --dev
fi

mkdir -p "$OUT_DIR"
echo "Building API docs into $OUT_DIR ..."
uv run pdoc \
  --output-directory "$OUT_DIR" \
  --docformat google \
  --no-search \
  agent_forge

echo "Done. Open: $OUT_DIR/agent_forge.html"
