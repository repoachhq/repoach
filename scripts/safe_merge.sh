#!/usr/bin/env bash
# scripts/safe_merge.sh — gated wrapper around ``gh pr merge``.
#
# When the GitHub Actions budget is exhausted (the workflows fail in 4 s)
# the merge gate must live client-side.  This wrapper enforces, in order:
#
# 1. PR base is ``develop`` (never ``main`` — convention says the user
#    does ``develop → main`` manually).
# 2. Local checkout sync : check out the PR's head branch and fast-merge
#    the current ``origin/develop`` tip into it, so the gates below test
#    the post-merge state, not the PR's branch in isolation
#    (SP-SAFE-MERGE-AUTO-CHECKOUT — the prior version ran
#    ``ci_local.sh`` against the user's current working tree, which
#    could be on ``develop`` instead of the PR's branch and would
#    silently test the wrong code).
# 3. ``scripts/ci_local.sh`` (full parity with .github/workflows/ci.yml).
# 4. ``repoach review pr <N>`` (review-bot team runs locally over NIM)
#    — refreshes the findings ledger at head before the gate reads it.
# 5. Pure evidence-first merge gate (SP-VERDICT-FLIP 10a):
#    ``repoach review gate <N>`` re-verifies the ledger at head and
#    decides on facts (CI green, zero blocking findings surviving
#    re-verification, complete review, spec coverage), NOT the forgeable
#    archive 4/4-APPROVE verdict the prior version parsed. Exit 0 →
#    merge-ready; exit 5 → refused (reasons printed, emergency override
#    prompt); exit 1 → could-not-evaluate, retried with exponential
#    backoff (rate limits / network) before failing loudly. The local
#    checkout is at the PR head (step 2), so the gate's on-disk
#    re-verification is correct here.
# 6. Fresh-head guard (SP-AUTOMERGE-FRESH-HEAD): cross-check the API
#    ``headRefOid`` against ``git ls-remote origin refs/heads/<head>``
#    immediately before the merge. A mismatch aborts with both SHAs
#    printed and NO emergency-override prompt — stale data is not
#    overridable.
# 7. ``gh pr merge <N> --squash --delete-branch``.
#
# On any exit path (success or gate failure), the script restores the
# original branch — your working tree returns to whatever you were on
# before the run.
#
# Usage:
#   scripts/safe_merge.sh <PR_NUMBER>
#   scripts/safe_merge.sh <PR_NUMBER> --skip-tests   # skip pytest (lint-only)
#   scripts/safe_merge.sh <PR_NUMBER> --skip-review  # skip review-bot team
#
# --skip-tests and --skip-review each disable part of the gate. When
# either is set the script prints a loud bypass warning and refuses to
# proceed until the operator types 'I understand' at the prompt; a
# non-interactive run (no tty) reads EOF and aborts (fails closed).
#
# Exit codes :
#   0 — merged successfully.
#   1 — a gate failed and the user did not authorise the override.
#   2 — usage error.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]] ; then
    echo "usage: $0 <PR_NUMBER> [--skip-tests | --skip-review]" >&2
    exit 2
fi

pr_number="$1"
shift

skip_tests="no"
skip_review="no"
while [[ $# -gt 0 ]] ; do
    case "$1" in
        --skip-tests)  skip_tests="yes" ;;
        --skip-review) skip_review="yes" ;;
        *) echo "unknown arg: $1" >&2 ; exit 2 ;;
    esac
    shift
done

bold() { printf "\n\033[1m== %s ==\033[0m\n" "$1" ; }
ok()   { printf "\033[32m  ✓ %s\033[0m\n" "$1" ; }
fail() { printf "\033[31m  ✗ %s\033[0m\n" "$1" ; }

if [[ "$skip_review" == "yes" || "$skip_tests" == "yes" ]] ; then
    printf "\n\033[1;31m!! merge-gate bypass requested !!\033[0m\n"
    if [[ "$skip_review" == "yes" ]] ; then
        printf "  --skip-review disables the review-bot team AND the evidence-first\n"
        printf "  merge gate — NO automated gate will evaluate PR #%s.\n" "$pr_number"
    fi
    if [[ "$skip_tests" == "yes" ]] ; then
        printf "  --skip-tests runs CI lint-only (the pytest matrix is skipped).\n"
    fi
    printf "This merges into \033[1mdevelop\033[0m with the checks above DISABLED.\n"
    printf "Type '\033[1mI understand\033[0m' to proceed, anything else aborts: "
    read -r bypass_ack || bypass_ack=""
    if [[ "$bypass_ack" != "I understand" ]] ; then
        echo "Aborted — bypass not confirmed."
        exit 1
    fi
    printf "\033[33mBypass confirmed by operator — proceeding.\033[0m\n"
fi

original_branch=$(git symbolic-ref --short -q HEAD || echo "")

restore_branch() {
    local target="$1"
    if [[ -z "$target" ]] || [[ "$target" == "(detached)" ]] ; then
        return
    fi
    if [[ "$(git symbolic-ref --short -q HEAD || echo "")" == "$target" ]] ; then
        return
    fi
    git checkout "$target" 2>/dev/null || true
}

cleanup_on_exit() {
    local exit_code=$?
    if [[ "$exit_code" -ne 0 ]] ; then
        git merge --abort 2>/dev/null || true
        restore_branch "$original_branch"
    fi
    return "$exit_code"
}
trap cleanup_on_exit EXIT

bold "[1/7] PR base must be develop"
base_ref=$(gh pr view "$pr_number" --json baseRefName --jq .baseRefName)
state=$(gh pr view "$pr_number" --json state --jq .state)
head_ref=$(gh pr view "$pr_number" --json headRefName --jq .headRefName)
if [[ "$state" != "OPEN" ]] ; then
    fail "PR #$pr_number state is $state, not OPEN"
    exit 1
fi
if [[ "$base_ref" != "develop" ]] ; then
    fail "PR #$pr_number targets $base_ref, not develop"
    exit 1
fi
ok "base=$base_ref state=$state head=$head_ref"

bold "[2/7] sync local checkout with PR head + origin/develop"
if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet 2>/dev/null ; then
    fail "uncommitted changes detected — commit or stash before safe_merge"
    git status --short
    exit 1
fi
git fetch origin --quiet
gh pr checkout "$pr_number"
merged_branch=$(git symbolic-ref --short HEAD)
if [[ "$merged_branch" != "$head_ref" ]] ; then
    fail "expected local branch $head_ref after gh pr checkout, got $merged_branch"
    exit 1
fi
if ! git merge --no-edit origin/develop ; then
    fail "merge conflict between origin/develop and PR branch — resolve manually"
    exit 1
fi
ok "checked out $head_ref + merged origin/develop"

bold "[3/7] scripts/ci_local.sh"
if [[ "$skip_tests" == "yes" ]] ; then
    ./scripts/ci_local.sh --fast
else
    ./scripts/ci_local.sh
fi

bold "[4/7] repoach review pr $pr_number"
if [[ "$skip_review" == "yes" ]] ; then
    echo "  (skipped via --skip-review — verdict check also skipped)"
else
    repoach review pr "$pr_number"
fi

bold "[5/7] pure merge gate (repoach review gate)"
if [[ "$skip_review" == "yes" ]] ; then
    echo "  (skipped — --skip-review implies the user accepts no automated gate)"
else
    gate_max_attempts=5
    gate_delay_s=10
    gate_attempt=1
    mkdir -p tmp
    gate_err="tmp/safe_merge_gate_err_${pr_number}.txt"
    while : ; do
        set +e
        gate_json=$(repoach review gate "$pr_number" 2>"$gate_err")
        gate_rc=$?
        set -e

        if [[ "$gate_rc" -eq 0 ]] ; then
            ok "pure gate satisfied — merge-ready"
            printf '%s' "$gate_json" | python -c 'import json,sys
d = json.loads(sys.stdin.read())
print("  head="+(d.get("head_sha") or "?")[:12]+"  facts="+json.dumps(d.get("facts", {})))' 2>/dev/null || true
            break
        fi

        if [[ "$gate_rc" -eq 5 ]] ; then
            fail "pure gate refused the merge"
            printf '%s' "$gate_json" | python -c 'import json,sys
d = json.loads(sys.stdin.read())
for r in d.get("reasons", []):
    print("    - "+r)' 2>/dev/null || printf '%s\n' "$gate_json"
            echo
            echo "The evidence-first gate blocks this merge. Resolve the reasons above and re-run."
            echo "Override (emergency only) ? Type 'merge' to proceed, anything else to abort."
            read -r confirm
            if [[ "$confirm" != "merge" ]] ; then
                echo "Aborted by user."
                exit 1
            fi
            echo "Override accepted — proceeding."
            break
        fi

        raw_dump="tmp/safe_merge_gate_raw_${pr_number}_${gate_attempt}.txt"
        printf '%s\n' "$gate_json" > "$raw_dump"
        if [[ "$gate_attempt" -ge "$gate_max_attempts" ]] ; then
            fail "could not evaluate the pure gate after $gate_max_attempts attempts (rc=$gate_rc)"
            sed 's/^/    /' "$gate_err" 2>/dev/null || true
            exit 1
        fi
        echo "  gate evaluation failed (rc=$gate_rc)" \
             "— retry $gate_attempt/$((gate_max_attempts - 1)) in ${gate_delay_s}s"
        echo "  raw capture saved to $raw_dump"
        sleep "$gate_delay_s"
        gate_delay_s=$((gate_delay_s * 2))
        gate_attempt=$((gate_attempt + 1))
    done
fi

bold "[6/7] fresh-head guard (SP-AUTOMERGE-FRESH-HEAD)"
api_head=$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)
remote_head=$(git ls-remote origin "refs/heads/$head_ref" | awk '{print $1}')
if [[ -z "$api_head" ]] || [[ -z "$remote_head" ]] || [[ "$api_head" != "$remote_head" ]] ; then
    fail "stale head detected — refusing to merge"
    echo "    api_head=$api_head"
    echo "    remote_head=$remote_head"
    echo "    head_ref=$head_ref"
    echo "Stale data is not overridable. Re-run after the API catches up."
    exit 1
fi
ok "head verified api=remote=$api_head"

bold "[7/7] gh pr merge --squash"
gh pr merge "$pr_number" --squash --delete-branch
ok "PR #$pr_number merged into develop"

git fetch origin --quiet
restore_branch "develop"
git pull --ff-only origin develop 2>/dev/null || true
ok "local develop synced with origin"
