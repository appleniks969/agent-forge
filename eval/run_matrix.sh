#!/usr/bin/env bash
# run_matrix.sh — run a full eval matrix: N reps × {forge|flow} × {thinking modes}
#
# Usage:
#   ./run_matrix.sh --task rate_limiter -n 3
#   ./run_matrix.sh --task rate_limiter -n 5 --conditions forge:adaptive,forge:off,flow:default
#
# Produces eval/results/<timestamp>/ with all runs + summary.{json,md}.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK="rate_limiter"
N=3
CONDITIONS="forge:adaptive,forge:off,flow:default"
RESULTS_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)        TASK="$2"; shift 2;;
    -n|--reps)     N="$2"; shift 2;;
    --conditions)  CONDITIONS="$2"; shift 2;;
    --results-dir) RESULTS_DIR="$2"; shift 2;;
    -h|--help)     sed -n '2,10p' "$0"; exit 2;;
    *) echo "unknown: $1"; exit 2;;
  esac
done

if [[ -z "$RESULTS_DIR" ]]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  RESULTS_DIR="$REPO_ROOT/eval/results/$TS"
fi
mkdir -p "$RESULTS_DIR"
echo "[matrix] results: $RESULTS_DIR"
echo "[matrix] task=$TASK n=$N conditions=$CONDITIONS"

IFS=',' read -ra COND_ARR <<< "$CONDITIONS"
TOTAL=$(( ${#COND_ARR[@]} * N ))
i=0
for cond in "${COND_ARR[@]}"; do
  TOOL="${cond%%:*}"
  THINK="${cond##*:}"
  [[ "$THINK" == "default" ]] && THINK_ARG="" || THINK_ARG="$THINK"
  for r in $(seq 1 "$N"); do
    i=$((i+1))
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "[$i/$TOTAL] tool=$TOOL thinking=${THINK_ARG:-default} run=$r"
    echo "════════════════════════════════════════════════════════════════"
    bash "$REPO_ROOT/eval/harness.sh" \
      --task "$TASK" --tool "$TOOL" --run-index "$r" \
      ${THINK_ARG:+--thinking "$THINK_ARG"} \
      --results-dir "$RESULTS_DIR" || echo "[matrix] run failed (continuing)"
  done
done

echo ""
echo "[matrix] aggregating…"
python3 "$REPO_ROOT/eval/aggregate.py" --results-dir "$RESULTS_DIR"
echo ""
echo "[matrix] done. open $RESULTS_DIR/summary.md"
