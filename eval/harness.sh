#!/usr/bin/env bash
# harness.sh — run agent-forge or agent-flow on a task, capture all metrics.
#
# Usage:
#   ./harness.sh --task rate_limiter --tool forge --run-index 1 [--thinking adaptive] [--results-dir DIR]
#   ./harness.sh --task rate_limiter --tool flow  --run-index 1 [--thinking off]      [--results-dir DIR]
#
# Outputs:
#   <results-dir>/runs/<tool>_<thinking>_<NN>.json
#   <results-dir>/raw/<tool>_<thinking>_<NN>.{stdout,stderr,time,workdir}
#
# Isolation: each run gets its own fresh /tmp/af-eval-<random> workdir.
# Memory cleared per skill rules.

set -uo pipefail

# ── Defaults ───────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK=""
TOOL=""
RUN_INDEX=""
THINKING=""           # "" → use tool default
MODEL="claude-sonnet-4-6"
RESULTS_DIR=""
PYTEST_BIN=""

usage() { sed -n '2,15p' "$0"; exit 2; }

# ── Arg parse ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)        TASK="$2"; shift 2;;
    --tool)        TOOL="$2"; shift 2;;
    --run-index)   RUN_INDEX="$2"; shift 2;;
    --thinking)    THINKING="$2"; shift 2;;
    --model)       MODEL="$2"; shift 2;;
    --results-dir) RESULTS_DIR="$2"; shift 2;;
    --pytest)      PYTEST_BIN="$2"; shift 2;;
    -h|--help)     usage;;
    *) echo "unknown arg: $1" >&2; usage;;
  esac
done

[[ -z "$TASK" || -z "$TOOL" || -z "$RUN_INDEX" ]] && usage
[[ "$TOOL" == "forge" || "$TOOL" == "flow" ]] || { echo "tool must be forge|flow"; exit 2; }

TASK_DIR="$REPO_ROOT/eval/tasks/$TASK"
[[ -f "$TASK_DIR/TASK.md" ]] || { echo "missing task: $TASK_DIR/TASK.md"; exit 2; }

[[ -z "$RESULTS_DIR" ]] && RESULTS_DIR="$REPO_ROOT/eval/results/latest"
mkdir -p "$RESULTS_DIR/runs" "$RESULTS_DIR/raw" "$RESULTS_DIR/workdirs"

# Tag: forge_adaptive_03  /  flow_default_01
THINK_TAG="${THINKING:-default}"
TAG="$(printf "%s_%s_%02d" "$TOOL" "$THINK_TAG" "$RUN_INDEX")"

# ── Per-run workdir (isolated) ─────────────────────────────────────────────
WORKDIR="$RESULTS_DIR/workdirs/$TAG"
rm -rf "$WORKDIR"
mkdir -p "$WORKDIR"
cp "$TASK_DIR/TASK.md" "$WORKDIR/TASK.md"

# ── Task type detection (python vs kotlin/gradle) ──────────────────────────
TASK_TYPE="python"
if [[ -d "$TASK_DIR/template" && -f "$TASK_DIR/template/build.gradle.kts" ]]; then
  TASK_TYPE="kotlin"
  # Copy template into workdir (preserve dotfiles, exclude build/.gradle)
  rsync -a --exclude=build --exclude=.gradle "$TASK_DIR/template/" "$WORKDIR/"
fi

# Set up venv + pytest if not provided
if [[ -z "$PYTEST_BIN" ]]; then
  if [[ -x "/tmp/harness-eval/venv/bin/pytest" ]]; then
    PYTEST_BIN="/tmp/harness-eval/venv/bin/pytest"
  else
    VENV_DIR="$RESULTS_DIR/.venv"
    [[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR" && "$VENV_DIR/bin/pip" install -q pytest
    PYTEST_BIN="$VENV_DIR/bin/pytest"
  fi
fi

# ── Memory clear (skill rule) ──────────────────────────────────────────────
rm -rf /tmp/.agent-forge/ /tmp/.agent-flow/ ~/.agent-flow/cache 2>/dev/null || true

# ── Build prompt — read TASK.md, prepend cwd hint ──────────────────────────
PROMPT="$(cat "$TASK_DIR/TASK.md")"

# ── Capture git sha for reproducibility ────────────────────────────────────
GIT_SHA="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# ── Output paths ───────────────────────────────────────────────────────────
STDOUT_F="$RESULTS_DIR/raw/$TAG.stdout"
STDERR_F="$RESULTS_DIR/raw/$TAG.stderr"
TIME_F="$RESULTS_DIR/raw/$TAG.time"
JSON_F="$RESULTS_DIR/runs/$TAG.json"

# ── Build CLI invocation ───────────────────────────────────────────────────
if [[ "$TOOL" == "forge" ]]; then
  CMD=(agent-forge --prompt "$PROMPT" --verbose --cwd "$WORKDIR" --model "$MODEL")
  [[ -n "$THINKING" ]] && CMD+=(--thinking "$THINKING")
else
  # agent-flow: --print non-interactive, --verbose for [api] line, --thinking optional
  CMD=(agent-flow --print --verbose --model "$MODEL")
  [[ -n "$THINKING" ]] && CMD+=(--thinking "$THINKING")
  CMD+=("$PROMPT")
fi

echo "[$TAG] running: ${CMD[*]:0:6} ... (cwd=$WORKDIR)" >&2

# ── Execute under /usr/bin/time wrapper ────────────────────────────────────
# zsh `time` keyword and bash `time` builtin behave differently — use coreutils gtime
# if available, else portable shell-builtin via TIMEFORMAT.
START_EPOCH=$(date +%s)
if [[ "$TOOL" == "flow" ]]; then
  # agent-flow needs to run in the workdir for its file-writing tools
  ( cd "$WORKDIR" && { time "${CMD[@]}" ; } ) >"$STDOUT_F" 2>"$STDERR_F"
  EXIT=$?
else
  # agent-forge takes --cwd, can be invoked from anywhere
  { time "${CMD[@]}" ; } >"$STDOUT_F" 2>"$STDERR_F"
  EXIT=$?
fi
END_EPOCH=$(date +%s)

# bash `time` builtin writes 'real Xm Y.YYYs' to stderr — extract and convert.
# Capture it from the stderr we just wrote (last 5 lines).
{
  awk '
    /real[[:space:]]+[0-9]/ {
      # Format: "real    0m64.180s"  → seconds = m*60 + s
      for (i=1; i<=NF; i++) {
        if ($i ~ /m[0-9.]+s$/) {
          split($i, a, "m"); m = a[1]+0; s = a[2]+0; sub(/s$/, "", s)
          printf "real %.3f\n", m*60+s
        }
      }
    }
    /user[[:space:]]+[0-9]/ {
      for (i=1; i<=NF; i++) {
        if ($i ~ /m[0-9.]+s$/) {
          split($i, a, "m"); m = a[1]+0; s = a[2]+0; sub(/s$/, "", s)
          printf "user %.3f\n", m*60+s
        }
      }
    }
    /sys[[:space:]]+[0-9]/ {
      for (i=1; i<=NF; i++) {
        if ($i ~ /m[0-9.]+s$/) {
          split($i, a, "m"); m = a[1]+0; s = a[2]+0; sub(/s$/, "", s)
          printf "sys %.3f\n", m*60+s
        }
      }
    }
  ' "$STDERR_F"
  echo "EXIT=$EXIT"
  echo "WALL_EPOCH_SECONDS=$((END_EPOCH - START_EPOCH))"
} > "$TIME_F"

# ── Parse run → JSON ───────────────────────────────────────────────────────
python3 "$REPO_ROOT/eval/parse_run.py" \
  --tool "$TOOL" \
  --stdout "$STDOUT_F" \
  --stderr "$STDERR_F" \
  --time "$TIME_F" \
  --task "$TASK" \
  --run-index "$RUN_INDEX" \
  --thinking "$THINKING" \
  --model "$MODEL" \
  --workdir "$WORKDIR" \
  --git-sha "$GIT_SHA" \
  > "$JSON_F.agent.json"

# ── Code metrics → JSON ────────────────────────────────────────────────────
if [[ "$TASK_TYPE" == "kotlin" ]]; then
  KT_HIDDEN_DIR="$TASK_DIR/hidden_tests"
  KT_HIDDEN_ARG=""
  [[ -d "$KT_HIDDEN_DIR" ]] && KT_HIDDEN_ARG="--hidden-tests-dir $KT_HIDDEN_DIR"
  : "${EVAL_JAVA_HOME:=/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home}"

  python3 "$REPO_ROOT/eval/code_metrics_kt.py" \
    --workdir "$WORKDIR" \
    --java-home "$EVAL_JAVA_HOME" \
    --repo-root "$REPO_ROOT" \
    $KT_HIDDEN_ARG \
    > "$JSON_F.code.json" 2>"$RESULTS_DIR/raw/$TAG.code-metrics.stderr" \
    || echo '{"_metrics_error": "code_metrics_kt failed"}' > "$JSON_F.code.json"
else
  HIDDEN_TESTS="$TASK_DIR/hidden_tests.py"
  HIDDEN_ARG=""
  [[ -f "$HIDDEN_TESTS" ]] && HIDDEN_ARG="--hidden-tests $HIDDEN_TESTS"

  RUFF_BIN="$(dirname "$PYTEST_BIN")/ruff"
  [[ -x "$RUFF_BIN" ]] || RUFF_BIN="ruff"

  python3 "$REPO_ROOT/eval/code_metrics.py" \
    --workdir "$WORKDIR" \
    --venv-pytest "$PYTEST_BIN" \
    --ruff-bin "$RUFF_BIN" \
    $HIDDEN_ARG \
    > "$JSON_F.code.json" 2>/dev/null || echo '{"_metrics_error": "code_metrics failed"}' > "$JSON_F.code.json"
fi

# ── Merge agent + code records ─────────────────────────────────────────────
python3 - "$JSON_F.agent.json" "$JSON_F.code.json" "$JSON_F" <<'PY'
import json, sys
agent = json.load(open(sys.argv[1]))
code  = json.load(open(sys.argv[2]))
merged = {**agent, "code": code}
json.dump(merged, open(sys.argv[3], "w"), indent=2)
PY
rm -f "$JSON_F.agent.json" "$JSON_F.code.json"

echo "[$TAG] done: $JSON_F (exit=$EXIT)" >&2
exit $EXIT
