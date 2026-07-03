#!/usr/bin/env bash
# scripts/hooks/git-wrapper.sh — Non-bypassable Tier 2 pre-push enforcement
#
# SOURCE this file from ~/.bashrc (done automatically by install-hooks.sh):
#   source /path/to/repo/scripts/hooks/git-wrapper.sh
#
# Effect: defines a `git` shell function that intercepts `git push --no-verify`
# on protected branches (spec/*, develop, main) and refuses with exit 1.
#
# Emergency bypass: EMERGENCY_PUSH=1 git push ...
#   - Requires a reason (minimum 20 chars) logged to ~/.push-audit.log
#   - Non-interactive contexts (no TTY) are refused even with the var set.
#
# Install: run ./scripts/install-hooks.sh
# Uninstall: remove the # tier2-wrapper BEGIN/END block from ~/.bashrc

_TIER2_WRAPPER_LOADED=1

git() {
    if [[ "$1" != "push" ]]; then
        command git "$@"
        return
    fi

    local current_branch
    current_branch="$(command git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"

    local protected=0
    case "$current_branch" in
        spec/* | develop | main)
            protected=1
            ;;
    esac

    if [[ "$protected" -eq 0 ]]; then
        command git "$@"
        return
    fi

    local has_no_verify=0
    for arg in "$@"; do
        if [[ "$arg" == "--no-verify" || "$arg" == "-n" ]]; then
            has_no_verify=1
            break
        fi
    done

    if [[ "$has_no_verify" -eq 0 ]]; then
        command git "$@"
        return
    fi

    if [[ -z "${EMERGENCY_PUSH:-}" ]]; then
        echo ""
        echo "==> --no-verify is DISABLED on protected branches." >&2
        echo "    Branch: $current_branch" >&2
        echo "    The pre-push hook enforces Tier 2 CI; --no-verify skips it." >&2
        echo "" >&2
        echo "    If Tier 2 fails and you have a genuine hook bug:" >&2
        echo "      EMERGENCY_PUSH=1 git push ..." >&2
        echo "    You will be prompted for a reason (>=20 chars, audited)." >&2
        echo ""
        return 1
    fi

    if [[ ! -t 0 ]]; then
        echo "Emergency push refused: no TTY available (cannot prompt for reason)." >&2
        echo "Set EMERGENCY_PUSH=1 only in interactive shells." >&2
        return 1
    fi

    echo ""
    echo "EMERGENCY PUSH — branch: $current_branch" >&2
    echo "This bypass is audited. Provide a reason (minimum 20 characters)." >&2
    echo -n "Reason: " >&2
    local reason
    IFS= read -r reason

    if [[ ${#reason} -lt 20 ]]; then
        echo "Reason too short (${#reason} chars < 20 required). Push aborted." >&2
        return 1
    fi

    local audit_log="$HOME/.push-audit.log"
    local sha
    sha="$(command git rev-parse HEAD 2>/dev/null || echo "unknown")"
    local timestamp
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    {
        echo "---"
        echo "timestamp: $timestamp"
        echo "branch: $current_branch"
        echo "sha: $sha"
        echo "reason: $reason"
        echo "user: ${USER:-unknown}@$(hostname -s 2>/dev/null || echo unknown)"
    } >> "$audit_log"

    echo ""
    echo "Bypass logged to $audit_log" >&2
    echo ""

    command git "$@"
}

export -f git 2>/dev/null || true
