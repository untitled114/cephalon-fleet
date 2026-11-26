#!/usr/bin/env bash
# The definition of "passing" for this repository. CI runs exactly this script.
#
#   scripts/check.sh [--stage amir|lumen|all]     (default: all)
#
# Every stage builds its environment under .ci/ from pinned requirements only, on a uv-managed
# Python, so a laptop and CI resolve the same interpreter and packages.
#   amir    Amir's own project: tests with his coverage gate, ruff check and format
#   lumen   Lumen's own project: tests at 96% coverage, ruff 0.9.7 check and format
# A skipped test fails its stage. Exit status 0 only if every selected stage passes.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CI="$ROOT/.ci"
export UV_PYTHON_PREFERENCE=only-managed
# Every pytest run loads scripts/check_no_skips.py: a skipped test fails its stage.
export PYTHONPATH="$ROOT/scripts"
PYTEST=(-m pytest -q -p no:cacheprovider -p check_no_skips)

stage="all"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stage) stage="${2:?--stage needs a name}"; shift ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# mkenv NAME PYTHON REQUIREMENTS: an environment that matches REQUIREMENTS exactly.
mkenv() {
    local dir="$CI/$1"
    uv venv --quiet --allow-existing --python "$2" "$dir" &&
        uv pip sync --quiet --python "$dir/bin/python" "$ROOT/$3" &&
        echo "$dir/bin/python"
}

run_amir() {
    cd "$ROOT/amir" &&
        UV_PROJECT_ENVIRONMENT="$CI/amir" uv sync --quiet --frozen &&
        "$CI/amir/bin/python" "${PYTEST[@]}" &&
        "$CI/amir/bin/ruff" check . &&
        "$CI/amir/bin/ruff" format --check .
}

run_lumen() {
    local py
    py="$(mkenv lumen 3.12 lumen-bot/constraints.txt)" || return 1
    cd "$ROOT/lumen-bot" &&
        "$py" "${PYTEST[@]}" tests \
            --cov=. --cov-report=term-missing:skip-covered --cov-fail-under=96 &&
        uvx --quiet ruff@0.9.7 check . &&
        uvx --quiet ruff@0.9.7 format --check .
}

case "$stage" in
    all) stages=(amir lumen) ;;
    amir|lumen) stages=("$stage") ;;
    *) echo "unknown stage: $stage" >&2; exit 2 ;;
esac

failed=()
for s in "${stages[@]}"; do
    echo "=== $s"
    if ( "run_$s" ); then
        echo "PASS $s"
    else
        echo "FAIL $s"
        failed+=("$s")
    fi
done
if [[ ${#failed[@]} -gt 0 ]]; then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "ALL PASSED: ${stages[*]}"
