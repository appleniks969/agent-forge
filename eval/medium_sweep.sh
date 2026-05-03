#!/usr/bin/env bash
# medium_sweep.sh — add forge/medium runs to the existing results dirs so the
# new condition lines up with the prior matrix.
#
#   - rate_limiter:  3 runs   into eval/results/validate/
#   - compose_notes: 1 run    into the latest eval/results/kotlin-n1-*/
#
# Writes a status file we can poll from the parent shell.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RL_DIR="$REPO_ROOT/eval/results/validate"
KT_DIR="$(ls -dt "$REPO_ROOT"/eval/results/kotlin-n1-* | head -1)"

mkdir -p "$RL_DIR/runs" "$RL_DIR/raw" "$RL_DIR/workdirs"
mkdir -p "$KT_DIR/runs" "$KT_DIR/raw" "$KT_DIR/workdirs"

export EVAL_JAVA_HOME="/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home"

STATUS_DIR="$REPO_ROOT/eval/results/_medium_sweep_status"
mkdir -p "$STATUS_DIR"
LOG="$STATUS_DIR/sweep.log"
STATUS="$STATUS_DIR/sweep.status"
: > "$STATUS"
: > "$LOG"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

run_one() {
  local TASK="$1" RDIR="$2" IDX="$3" BUDGET="$4"
  local TAG="forge_medium_$(printf '%02d' "$IDX")"
  local START=$(date +%s)
  log "START  $TASK / $TAG  (budget=${BUDGET}s)"
  echo "STARTED $TASK $TAG $START" >> "$STATUS"

  perl -e 'alarm shift; exec @ARGV' "$BUDGET" \
    bash "$REPO_ROOT/eval/harness.sh" \
      --task "$TASK" \
      --tool forge \
      --thinking medium \
      --run-index "$IDX" \
      --results-dir "$RDIR" \
      >>"$LOG" 2>&1
  local RC=$?
  local DUR=$(( $(date +%s) - START ))
  if [[ $RC -eq 0 ]]; then
    log "DONE   $TASK / $TAG  (${DUR}s, exit 0)"
    echo "DONE $TASK $TAG $DUR 0" >> "$STATUS"
  else
    log "FAIL   $TASK / $TAG  (${DUR}s, exit $RC)"
    echo "FAIL $TASK $TAG $DUR $RC" >> "$STATUS"
  fi
}

# rate_limiter: cheap and quick, do 3 runs.
run_one rate_limiter         "$RL_DIR" 1 300
run_one rate_limiter         "$RL_DIR" 2 300
run_one rate_limiter         "$RL_DIR" 3 300

# compose_notes: expensive, just 1 run for now (matches existing n=1).
run_one jetpack_compose_notes "$KT_DIR" 1 1500

log "aggregating rate_limiter..."
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$RL_DIR" >>"$LOG" 2>&1 || true
log "aggregating compose_notes..."
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$KT_DIR" >>"$LOG" 2>&1 || true
log "ALL DONE"
echo "ALL_DONE $(date +%s)" >> "$STATUS"
