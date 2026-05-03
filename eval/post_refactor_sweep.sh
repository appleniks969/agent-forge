#!/usr/bin/env bash
# post_refactor_sweep.sh — re-run the validated matrix after refactoring.
#
#   rate_limiter   (n=3): forge/off · forge/medium · forge/adaptive · flow/default
#   compose_notes  (n=1): forge/off · forge/medium · forge/adaptive · flow/default
#
# Status markers in $STATUS so the parent shell can poll progress.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"

RL_DIR="$REPO_ROOT/eval/results/post-refactor-rl-$STAMP"
KT_DIR="$REPO_ROOT/eval/results/post-refactor-kt-$STAMP"

mkdir -p "$RL_DIR/runs" "$RL_DIR/raw" "$RL_DIR/workdirs"
mkdir -p "$KT_DIR/runs" "$KT_DIR/raw" "$KT_DIR/workdirs"

export EVAL_JAVA_HOME="/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home"

LOG="$RL_DIR/sweep.log"
STATUS="$RL_DIR/sweep.status"
: > "$LOG"; : > "$STATUS"
echo "$KT_DIR" > "$RL_DIR/kt_dir.txt"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

run_one() {
  local TASK="$1" RDIR="$2" TOOL="$3" THINK="$4" IDX="$5" BUDGET="$6"
  local TAG="${TOOL}_${THINK}_$(printf '%02d' "$IDX")"
  local START=$(date +%s)
  log "START  $TASK / $TAG  (budget=${BUDGET}s)"
  echo "STARTED $TASK $TAG $START" >> "$STATUS"

  perl -e 'alarm shift; exec @ARGV' "$BUDGET" \
    bash "$REPO_ROOT/eval/harness.sh" \
      --task "$TASK" \
      --tool "$TOOL" \
      --thinking "$THINK" \
      --run-index "$IDX" \
      --results-dir "$RDIR" \
      >>"$LOG" 2>&1
  local RC=$?
  local DUR=$(( $(date +%s) - START ))
  if [[ $RC -eq 0 ]]; then
    log "DONE   $TASK / $TAG  (${DUR}s)"
    echo "DONE $TASK $TAG $DUR 0" >> "$STATUS"
  elif [[ $RC -eq 142 || $RC -eq 124 ]]; then
    log "TIMEOUT $TASK / $TAG (${DUR}s)"
    echo "TIMEOUT $TASK $TAG $DUR $RC" >> "$STATUS"
  else
    log "FAIL   $TASK / $TAG  (${DUR}s, exit $RC)"
    echo "FAIL $TASK $TAG $DUR $RC" >> "$STATUS"
  fi
}

# ── Pre-warm Gradle for the Kotlin task ──────────────────────────────────
log "pre-warming gradle..."
( cd "$REPO_ROOT/eval/tasks/jetpack_compose_notes/template" && \
  JAVA_HOME="$EVAL_JAVA_HOME" gradle --no-daemon compileKotlin compileTestKotlin -q 2>&1 ) \
  | tail -5 >> "$LOG" || true
log "pre-warm done"

# ── rate_limiter: 4 conditions × 3 runs = 12 ─────────────────────────────
for IDX in 1 2 3; do
  run_one rate_limiter "$RL_DIR" forge off      "$IDX" 300
  run_one rate_limiter "$RL_DIR" forge medium   "$IDX" 300
  run_one rate_limiter "$RL_DIR" forge adaptive "$IDX" 300
  run_one rate_limiter "$RL_DIR" flow  default  "$IDX" 300
done

# ── compose_notes: 4 conditions × 1 run = 4 ──────────────────────────────
run_one jetpack_compose_notes "$KT_DIR" forge off      1 1500
run_one jetpack_compose_notes "$KT_DIR" forge medium   1 1500
run_one jetpack_compose_notes "$KT_DIR" forge adaptive 1 1500
run_one jetpack_compose_notes "$KT_DIR" flow  default  1 1500

log "aggregating..."
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$RL_DIR" >>"$LOG" 2>&1 || true
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$KT_DIR" >>"$LOG" 2>&1 || true

log "ALL DONE"
echo "ALL_DONE $(date +%s)" >> "$STATUS"
