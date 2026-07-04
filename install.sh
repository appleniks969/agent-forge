#!/usr/bin/env bash
# install.sh — install forge from this repo and verify the install actually works.
#
# Usage:
#   ./install.sh              install + verify + offline smoke test
#   ./install.sh --live       also run one real model turn (needs credentials; costs ~$0.02)
#   ./install.sh --eval-deps  also report which eval-suite toolchains are present
#
# Safe to re-run. Always rebuilds from source: `uv tool install --force` alone
# reuses the cached wheel when the version number hasn't changed, silently
# installing stale code — --reinstall + a file-hash check below prevent that.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE=0
EVAL_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    --eval-deps) EVAL_DEPS=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (try --help)"; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓ \033[0m%s\n' "$*"; }
fail() { printf '\033[1;31m ✗ %s\033[0m\n' "$*"; exit 1; }

# --- 1. prerequisites ---------------------------------------------------------
say "Checking prerequisites"
command -v uv >/dev/null 2>&1 \
  || fail "uv is required. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
ok "uv $(uv --version | awk '{print $2}')"

# --- 2. install (always rebuild from source) -----------------------------------
say "Installing forge from $REPO_DIR"
uv tool install --force --reinstall "$REPO_DIR" >/dev/null 2>&1 \
  || fail "uv tool install failed — re-run without output suppression: uv tool install --force --reinstall $REPO_DIR"
ok "installed via uv tool"

# --- 3. PATH + binary check ----------------------------------------------------
if ! command -v forge >/dev/null 2>&1; then
  fail "forge is not on PATH. Add ~/.local/bin to PATH (uv installs executables there):
    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi
ok "forge on PATH: $(command -v forge)"

# --- 4. staleness check: installed code must match the source tree --------------
# The system-prompt policy file is the one that bit us; hash-compare it.
SRC_FILE="$REPO_DIR/forge/policy/prompt.py"
INST_FILE="$(find "$HOME/.local/share/uv/tools/forge/lib" -path '*/forge/policy/prompt.py' 2>/dev/null | head -1)"
[ -n "$INST_FILE" ] || fail "cannot locate installed forge package to verify"
if command -v shasum >/dev/null 2>&1; then HASH=shasum; else HASH=sha1sum; fi
if [ "$($HASH "$SRC_FILE" | awk '{print $1}')" != "$($HASH "$INST_FILE" | awk '{print $1}')" ]; then
  fail "installed copy is STALE (does not match source). This means the cached wheel was reused; re-run this script."
fi
ok "installed code matches source (no stale wheel)"

# --- 5. offline smoke test (no credentials, no network) -------------------------
say "Offline smoke test (scripted fake provider)"
SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
SMOKE_OUT="$(cd "$SMOKE_DIR" && forge run --provider fake --no-mcp --json -p "smoke test" 2>/dev/null)"
echo "$SMOKE_OUT" | grep -q '"outcome": "ok"' \
  || fail "smoke test did not return outcome=ok. Output: $SMOKE_OUT"
ok "one full turn through the kernel: outcome=ok"

# --- 6. optional live test -------------------------------------------------------
if [ "$LIVE" -eq 1 ]; then
  say "Live model test (one short prompt)"
  if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    fail "no credentials: export ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN first"
  fi
  LIVE_OUT="$(cd "$SMOKE_DIR" && forge run --no-mcp --json -p "Reply with exactly: forge live test ok" 2>/dev/null)"
  echo "$LIVE_OUT" | grep -q '"outcome": "ok"' \
    || fail "live test failed. Output: $LIVE_OUT"
  LIVE_SUMMARY="$(echo "$LIVE_OUT" | python3 -c 'import json,sys; j=json.load(sys.stdin); u=j["usage"]; print(str(j["turns"]) + " turn(s), " + str(u["output_tokens"]) + " output tokens")')"
  ok "live turn completed: $LIVE_SUMMARY"
else
  say "Skipping live model test (pass --live to run one real turn; needs credentials)"
fi

# --- 7. optional eval toolchain report -------------------------------------------
if [ "$EVAL_DEPS" -eq 1 ]; then
  say "Eval-suite toolchains (.claude/skills/agent-forge-eval)"
  for tool in tsc npx kotlinc swiftc; do
    if command -v "$tool" >/dev/null 2>&1; then
      ok "$tool ($(command -v "$tool"))"
    else
      printf '\033[1;33m ! \033[0m%s missing — %s\n' "$tool" \
        "$(case $tool in tsc|npx) echo 'npm install -g typescript / node';; kotlinc) echo 'brew install kotlin (only needed for C4)';; swiftc) echo 'xcode-select --install (only needed for C5)';; esac)"
    fi
  done
fi

say "Done. Try it:"
echo "    cd <your-project> && forge run -p \"your prompt\""
echo "    forge --help    # REPL, sessions, MCP flags"
