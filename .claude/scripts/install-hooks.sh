#!/usr/bin/env bash
# .claude/scripts/install-hooks.sh — wire git-wrapper.sh into ~/.bashrc.
#
# Sourcing the wrapper from ~/.bashrc enables non-bypassable
# `git push --no-verify` enforcement on protected branches
# (spec/*, develop, main).
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
WRAPPER="$REPO_ROOT/.claude/scripts/git-wrapper.sh"

if [ ! -f "$WRAPPER" ]; then
    echo ".claude/scripts/git-wrapper.sh not found." >&2
    exit 1
fi

chmod +x "$WRAPPER"

BASHRC="$HOME/.bashrc"
MARKER_BEGIN="# tier2-wrapper-ferova BEGIN"
MARKER_END="# tier2-wrapper-ferova END"

if grep -qF "$MARKER_BEGIN" "$BASHRC" 2>/dev/null; then
    echo "git-wrapper.sh already present in $BASHRC (skipping)."
else
    cat >> "$BASHRC" <<BASHRC_BLOCK

$MARKER_BEGIN
# Non-bypassable Tier 2 pre-push enforcement for repoach.
# Installed by: $REPO_ROOT/.claude/scripts/install-hooks.sh
# To uninstall: remove this block (BEGIN..END inclusive).
if [ -f "$WRAPPER" ]; then
    # shellcheck source=.claude/scripts/git-wrapper.sh
    source "$WRAPPER"
fi
$MARKER_END
BASHRC_BLOCK

    echo "git-wrapper.sh sourced from $BASHRC"
    echo "Open a new shell or run: source ~/.bashrc"
fi

echo ""
echo "Installation complete."
echo "  --no-verify on spec/*, develop, main -> refused (exit 1)"
echo "  Emergency bypass: EMERGENCY_PUSH=1 git push ... (audited to ~/.push-audit.log)"
