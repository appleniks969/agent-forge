#!/usr/bin/env bash
set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
DIM="\033[2m"
RESET="\033[0m"

info()    { echo -e "${BOLD}$*${RESET}"; }
ok()      { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
die()     { echo -e "${RED}✗${RESET}  $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
info "agent-forge installer"
echo -e "${DIM}  Source: $SCRIPT_DIR${RESET}"
echo ""

# ── 1. Check for uv ──────────────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    warn "uv not found. Installing via official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv &>/dev/null || die "uv install failed. Visit https://docs.astral.sh/uv/getting-started/installation/"
    ok "uv installed"
else
    ok "uv $(uv --version 2>&1 | awk '{print $2}') found"
fi

# ── 2. Install agent-forge as a uv tool ─────────────────────────────────────

info "Installing agent-forge..."
uv tool install "$SCRIPT_DIR" --reinstall

ok "agent-forge installed"

# ── 3. Ensure bin dir is on PATH ─────────────────────────────────────────────

BIN_DIR="$HOME/.local/bin"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not on PATH. Adding it now..."

    SHELL_NAME="$(basename "${SHELL:-bash}")"
    if [[ "$SHELL_NAME" == "zsh" ]]; then
        RC="$HOME/.zshrc"
    elif [[ "$SHELL_NAME" == "bash" ]]; then
        RC="$HOME/.bashrc"
    else
        RC=""
    fi

    EXPORT_LINE='export PATH="$HOME/.local/bin:$PATH"'

    if [[ -n "$RC" ]]; then
        if ! grep -qF "$EXPORT_LINE" "$RC" 2>/dev/null; then
            echo "" >> "$RC"
            echo "# agent-forge" >> "$RC"
            echo "$EXPORT_LINE" >> "$RC"
            ok "Added PATH entry to $RC"
        else
            ok "$RC already has PATH entry"
        fi
        export PATH="$BIN_DIR:$PATH"
    else
        warn "Unknown shell ($SHELL_NAME). Add this to your shell rc manually:"
        echo "  $EXPORT_LINE"
    fi
else
    ok "$BIN_DIR already on PATH"
fi

# ── 4. Smoke test ────────────────────────────────────────────────────────────

if command -v agent-forge &>/dev/null; then
    ok "agent-forge is ready"
else
    warn "agent-forge not found in current shell. Run:"
    echo -e "  ${DIM}source ~/.zshrc${RESET}  (or open a new terminal)"
fi

# ── 5. Done ──────────────────────────────────────────────────────────────────

echo ""
info "Done. Usage:"
echo -e "  ${DIM}cd /your/project && agent-forge${RESET}"
echo -e "  ${DIM}agent-forge --prompt \"explain this codebase\"${RESET}"
echo -e "  ${DIM}agent-forge --help${RESET}"
echo ""
