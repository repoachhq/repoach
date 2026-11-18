#!/usr/bin/env bash
# SP-SCRUB-PERSONAL-DATA-AT-HEAD D3: render the systemd --user units of
# deploy/systemd/ with an absolute checkout path, verify them, install them.
#
# Usage:
#   scripts/install_user_units.sh [--home ABS_PATH] [--dest DIR] [--dry-run]
#
# The tracked units are templates holding @REPOACH_HOME@; they are never
# copied as they are. --home defaults to the checkout this script lives in
# and must be absolute. --dest defaults to
# ${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user. The five files are
# rendered into a staging directory and verified with
# `systemd-analyze --user verify`; any output refuses the install (exit 2,
# output relayed, nothing written to --dest, nothing reloaded). Otherwise
# they are written to --dest, <home>/logs is created and
# `systemctl --user daemon-reload` runs when --dest is the user unit
# directory (the default); any other --dest prints
# `daemon-reload: skipped (--dest is not the user unit dir)` and reloads
# nothing, so a render into a scratch directory leaves the user manager
# alone. --dry-run prints the rendered
# files, each after a `# --- <name>` line, and writes nothing.
# Exit 2 names its reason on stderr.

set -euo pipefail

token="@REPOACH_HOME@"
units=(
    repoach-llm-proxy.service
    repoach-nim-health.service
    repoach-nim-health.timer
    repoach-chainpilot.service
    repoach-chainpilot.timer
)

refuse() {
    echo "install_user_units: $1" >&2
    exit 2
}

script_dir="$(cd "$(dirname "$0")" && pwd)"
src_dir="$script_dir/../deploy/systemd"
home=""
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
dest="$user_unit_dir"
dry_run=0

while [[ $# -gt 0 ]] ; do
    case "$1" in
        --home)    [[ $# -ge 2 ]] || refuse "--home needs a path" ; home="$2" ; shift 2 ;;
        --dest)    [[ $# -ge 2 ]] || refuse "--dest needs a directory" ; dest="$2" ; shift 2 ;;
        --dry-run) dry_run=1 ; shift ;;
        *)         refuse "unknown argument $1" ;;
    esac
done

if [[ -z "$home" ]] ; then
    home="$(git -C "$script_dir/.." rev-parse --show-toplevel 2>/dev/null)" \
        || refuse "not a git work tree; pass --home <absolute checkout path>"
fi
[[ "$home" == /* ]] || refuse "--home must be an absolute path"
home="${home%/}"
case "$home" in
    *"|"*|*$'\n'*) refuse "--home must not contain '|' or a newline" ;;
esac

home_sed="${home//\\/\\\\}"
home_sed="${home_sed//&/\\&}"

render() {
    sed "s|$token|$home_sed|g" "$src_dir/$1"
}

if [[ "$dry_run" -eq 1 ]] ; then
    for unit in "${units[@]}" ; do
        echo "# --- $unit"
        render "$unit"
    done
    exit 0
fi

command -v systemctl >/dev/null 2>&1 || refuse "systemctl not found: systemd --user is unavailable"
systemctl --user show-environment >/dev/null 2>&1 \
    || refuse "systemctl --user is unavailable: no user manager for this session"
command -v systemd-analyze >/dev/null 2>&1 || refuse "systemd-analyze not found: the units cannot be verified"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
for unit in "${units[@]}" ; do
    render "$unit" > "$stage/$unit"
done
if grep -l -F "$token" "$stage"/* >/dev/null ; then
    refuse "a rendered unit still holds $token"
fi

staged=()
for unit in "${units[@]}" ; do
    staged+=("$stage/$unit")
done
verify_out="$(systemd-analyze --user verify "${staged[@]}" 2>&1)" || true
if [[ -n "$verify_out" ]] ; then
    echo "$verify_out" >&2
    refuse "systemd-analyze --user verify printed the lines above; nothing installed"
fi

mkdir -p "$dest" "$home/logs"
for unit in "${units[@]}" ; do
    cp "$stage/$unit" "$dest/$unit"
    echo "installed: $dest/$unit"
done
for unit in "${units[@]}" ; do
    if [[ "$unit" == *.service ]] ; then
        echo "verified: $unit"
    fi
done
if [[ "$(cd "$dest" && pwd -P)" == "$(cd "$user_unit_dir" 2>/dev/null && pwd -P)" ]] ; then
    systemctl --user daemon-reload
    echo "daemon-reload: done"
else
    echo "daemon-reload: skipped (--dest is not the user unit dir)"
fi
