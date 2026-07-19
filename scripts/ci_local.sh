#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — run before pushing when the
# GitHub Actions budget is exhausted (or always, since the loop is faster
# than waiting on a runner anyway).
#
# Usage:
#   scripts/ci_local.sh            # full CI parity (lint + unit tests + integration tests)
#   scripts/ci_local.sh --fast     # skip pytest (lint-only — pre-commit-style)
#   scripts/ci_local.sh --tests    # pytest unit tests only (skip lint and integration)
#   scripts/ci_local.sh --integration  # pytest integration tests only
#   scripts/ci_local.sh --review <PR>  # mirror the .github/workflows/auto-review.yml
#                                      # review job locally — runs the four
#                                      # bots against the PR via the V2
#                                      # capability gateway and emits the
#                                      # same JSON contract the workflow
#                                      # consumes (SP-AUTO-REVIEW-V2-MIGRATION).
#
# Exits 0 only when every gate passes ; any failure aborts with the
# failing step's exit code.

set -euo pipefail

cd "$(dirname "$0")/.."

mode="full"
review_pr=""
case "${1:-}" in
    --fast)   mode="lint" ;;
    --tests)  mode="tests" ;;
    --integration) mode="integration" ;;
    --review)
        mode="review"
        review_pr="${2:-}"
        if [[ -z "$review_pr" ]] ; then
            echo "usage: $0 --review <PR_NUMBER>" >&2
            exit 2
        fi
        ;;
    "")       mode="full" ;;
    *)        echo "usage: $0 [--fast | --tests | --integration | --review <PR>]" >&2 ; exit 2 ;;
esac

bold() { printf "\n\033[1m== %s ==\033[0m\n" "$1" ; }
ok()   { printf "\033[32m  ✓ %s\033[0m\n" "$1" ; }
fail() { printf "\033[31m  ✗ %s\033[0m\n" "$1" ; }

failures=()

# SP-CI-SMOKE-REPAIR: mirror of the ci.yml "Smoke — llm_proxy boots"
# step. Boots the proxy with a dummy token on loopback, polls /health
# with a bounded retry loop, and dumps the captured log on failure.
proxy_smoke() {
    mkdir -p logs
    FEROVA_ANTHROPIC_AUTH_TOKEN="ci-smoke-token" \
    FEROVA_PROXY_HOST="127.0.0.1" \
    FEROVA_PROXY_PORT="8082" \
        python -m repoach.llm_proxy > logs/proxy_smoke.log 2>&1 &
    local proxy_pid=$!
    local i
    for i in $(seq 1 15) ; do
        if curl --silent --fail --max-time 2 http://127.0.0.1:8082/health > /dev/null ; then
            echo "  proxy /health answered (attempt $i)"
            kill "$proxy_pid" 2>/dev/null || true
            return 0
        fi
        sleep 2
    done
    echo "  llm_proxy never answered /health within 30s — log tail:" >&2
    tail -50 logs/proxy_smoke.log >&2 || true
    kill "$proxy_pid" 2>/dev/null || true
    return 1
}

run_step() {
    local label="$1"
    shift
    bold "$label"
    if "$@" ; then
        ok "$label"
    else
        fail "$label"
        failures+=("$label")
    fi
}

if [[ "$mode" == "review" ]] ; then
    artifact_dir="logs/review_ci"
    mkdir -p "$artifact_dir"
    artifact_path="$artifact_dir/review_pr_${review_pr}.json"
    bold "ferova review pr $review_pr (--ci-mode mirror)"
    set +e
    ferova review pr "$review_pr" --dry-run > "$artifact_path"
    rc=$?
    set -e
    if [[ $rc -eq 0 ]] ; then
        ok "review pr exited 0 (APPROVE or COMMENT)"
    elif [[ $rc -eq 2 ]] ; then
        ok "review pr exited 2 (REQUEST_CHANGES — verdict captured)"
    else
        fail "review pr exited $rc (pipeline error)"
        failures+=("ferova review pr $review_pr")
    fi
    echo "  artifact: $artifact_path"
    bold "Summary"
    if [[ ${#failures[@]} -eq 0 ]] ; then
        ok "review captured to $artifact_path"
        exit 0
    fi
    fail "${#failures[@]} gate(s) failed:"
    printf '    - %s\n' "${failures[@]}"
    exit 1
fi

if [[ "$mode" != "tests" && "$mode" != "integration" ]] ; then
    run_step "ruff check"        ruff check src tests
    run_step "ruff format --check" ruff format --check src tests
    run_step "lint — no inline comments / no noqa" python scripts/lint_no_inline_comments.py --summary
    run_step "lint — no silent except" python scripts/lint_no_silent_except.py --summary
fi

if [[ "$mode" == "full" || "$mode" == "tests" ]] ; then
    run_step "pytest tests/unit" python -m pytest -q tests/unit
fi

if [[ "$mode" == "full" || "$mode" == "integration" ]] ; then
    if [[ -n "$(find tests/integration -name 'test_*.py' 2>/dev/null)" ]] ; then
        run_step "pytest tests/integration" python -m pytest -q tests/integration
    else
        bold "pytest tests/integration"
        ok "no integration tests found — stage skipped"
    fi
fi

if [[ "$mode" == "full" ]] ; then
    bold "smoke — llm_proxy boots"
    # Never boot the smoke proxy on a port that is already bound: on
    # the operator box 8082 is usually the live systemd proxy and the
    # smoke's kill would take it down.
    if ss -tlnp 2>/dev/null | grep -q ':8082 ' ; then
        ok "skipped — port 8082 already bound (live proxy left untouched)"
    elif proxy_smoke ; then
        ok "smoke — llm_proxy boots"
    else
        fail "smoke — llm_proxy boots"
        failures+=("smoke — llm_proxy boots")
    fi
fi

bold "Summary"
if [[ ${#failures[@]} -eq 0 ]] ; then
    ok "all gates passed"
    exit 0
fi
fail "${#failures[@]} gate(s) failed:"
printf '    - %s\n' "${failures[@]}"
exit 1
