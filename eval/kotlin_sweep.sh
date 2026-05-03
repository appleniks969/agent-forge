#!/usr/bin/env bash
# kotlin_sweep.sh — run forge/adaptive, forge/off, flow/default once each on
# the jetpack_compose_notes task, with a per-run wall-time cap. Writes status
# markers so a parent process can poll progress.
#
# Usage:
#   ./kotlin_sweep.sh <results-dir>

set -uo pipefail

RESULTS_DIR="${1:-}"
[[ -z "$RESULTS_DIR" ]] && { echo "usage: $0 <results-dir>" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$RESULTS_DIR" "$RESULTS_DIR/runs" "$RESULTS_DIR/raw" "$RESULTS_DIR/workdirs"

export EVAL_JAVA_HOME="/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home"

PER_RUN_BUDGET_SEC=1500   # 25 minutes — kill agent if it stalls
SWEEP_LOG="$RESULTS_DIR/sweep.log"
STATUS="$RESULTS_DIR/sweep.status"
: > "$STATUS"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$SWEEP_LOG"; }

# ── Pre-warm gradle once so agent runs don't pay download cost ────────────
log "pre-warming gradle in template..."
( cd "$REPO_ROOT/eval/tasks/jetpack_compose_notes/template" && \
  JAVA_HOME="$EVAL_JAVA_HOME" gradle --no-daemon compileKotlin compileTestKotlin -q 2>&1 ) \
  | tail -5 >> "$SWEEP_LOG" || true
log "pre-warm done"

run_one() {
  local TOOL="$1" THINK="$2" TAG="$3"
  local START=$(date +%s)
  log "START  $TAG  (budget=${PER_RUN_BUDGET_SEC}s)"
  echo "STARTED $TAG $START" >> "$STATUS"

  # Watchdog: kill harness if it exceeds the budget.
  perl -e 'alarm shift; exec @ARGV' "$PER_RUN_BUDGET_SEC" \
    bash "$REPO_ROOT/eval/harness.sh" \
      --task jetpack_compose_notes \
      --tool "$TOOL" \
      --thinking "$THINK" \
      --run-index 1 \
      --results-dir "$RESULTS_DIR" \
      >>"$SWEEP_LOG" 2>&1
  local RC=$?
  local DURATION=$(( $(date +%s) - START ))

  if [[ $RC -eq 0 ]]; then
    log "DONE   $TAG  (${DURATION}s, exit 0)"
    echo "DONE $TAG $DURATION 0" >> "$STATUS"
  elif [[ $RC -eq 142 || $RC -eq 124 ]]; then
    log "TIMEOUT $TAG (${DURATION}s, killed by watchdog)"
    echo "TIMEOUT $TAG $DURATION $RC" >> "$STATUS"
  else
    log "FAIL   $TAG  (${DURATION}s, exit $RC)"
    echo "FAIL $TAG $DURATION $RC" >> "$STATUS"
  fi
}

run_one forge adaptive forge_adaptive_01
run_one forge off      forge_off_01
run_one flow  default  flow_default_01

# ── Aggregate ──────────────────────────────────────────────────────────────
log "aggregating..."
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$RESULTS_DIR" >>"$SWEEP_LOG" 2>&1 || true
log "ALL DONE"
echo "ALL_DONE $(date +%s)" >> "$STATUS"
