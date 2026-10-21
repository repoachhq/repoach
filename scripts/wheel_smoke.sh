#!/usr/bin/env bash
# SP-PYPI-RELEASE G2: install a built repoach with pipx in a clean HOME,
# outside any checkout, and run it.
#
# Usage:
#   scripts/wheel_smoke.sh --python PY [--expect VERSION] [--index-url URL] TARGET
#
# TARGET is a wheel path (dist/repoach-*.whl) or a requirement
# (repoach==0.1.0, installed from --index-url). VERSION defaults to the
# version in the wheel's file name. Every command must exit 0:
#   repoach --version   prints exactly "repoach VERSION"
#   repoach --help
#   repoach doctor      in an empty git repository, provider ollama
#   repoach init --ci gitlab --print   pins repoach==VERSION
# PIPX, when set, names the pipx command (default: pipx).

set -euo pipefail

python_bin=""
expect=""
index_url=""
target=""

while [[ $# -gt 0 ]] ; do
    case "$1" in
        --python)    python_bin="$2" ; shift 2 ;;
        --expect)    expect="$2" ; shift 2 ;;
        --index-url) index_url="$2" ; shift 2 ;;
        -*)          echo "wheel_smoke: unknown option $1" >&2 ; exit 2 ;;
        *)           target="$1" ; shift ;;
    esac
done

if [[ -z "$python_bin" || -z "$target" ]] ; then
    echo "usage: $0 --python PY [--expect VERSION] [--index-url URL] TARGET" >&2
    exit 2
fi

if [[ "$target" == *.whl ]] ; then
    if [[ ! -f "$target" ]] ; then
        echo "wheel_smoke: no such wheel: $target" >&2
        exit 2
    fi
    target="$(cd "$(dirname "$target")" && pwd)/$(basename "$target")"
    if [[ -z "$expect" ]] ; then
        expect="$(basename "$target" | cut -d- -f2)"
    fi
fi

if [[ -z "$expect" ]] ; then
    echo "wheel_smoke: --expect is required when TARGET is not a wheel" >&2
    exit 2
fi

read -r -a pipx_cmd <<< "${PIPX:-pipx}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/home" "$work/empty" "$work/repo"

export HOME="$work/home"
export PIPX_HOME="$work/pipx"
export PIPX_BIN_DIR="$work/bin"
export XDG_CONFIG_HOME="$work/home/.config"
export XDG_DATA_HOME="$work/home/.local/share"
unset PYTHONPATH VIRTUAL_ENV PIP_INDEX_URL PIP_EXTRA_INDEX_URL
unset REPOACH_PROVIDER REPOACH_LLM_PROXY_BASE_URL REPOACH_ANTHROPIC_API_KEY ANTHROPIC_API_KEY

install_args=(install --python "$python_bin")
if [[ -n "$index_url" ]] ; then
    install_args+=(--index-url "$index_url")
fi

echo "== pipx ${install_args[*]} $target"
"${pipx_cmd[@]}" "${install_args[@]}" "$target"

repoach="$PIPX_BIN_DIR/repoach"
cd "$work/empty"

echo "== repoach --version"
printed="$("$repoach" --version)"
echo "$printed"
if [[ "$printed" != "repoach $expect" ]] ; then
    echo "version mismatch: wheel ${printed#repoach }, expected $expect" >&2
    exit 1
fi

echo "== repoach --help"
"$repoach" --help > /dev/null
echo "ok"

cd "$work/repo"
git init -q

echo "== repoach doctor (empty git repository, provider ollama)"
REPOACH_PROVIDER=ollama \
REPOACH_MODEL_OPUS=smoke REPOACH_MODEL_SONNET=smoke REPOACH_MODEL_HAIKU=smoke \
REPOACH_FORGE=github \
    "$repoach" doctor > "$work/doctor.out"
grep -E '^(repoach=|project_root=|forge=|provider=)' "$work/doctor.out"

echo "== repoach init --ci gitlab --print"
"$repoach" init --ci gitlab --print > "$work/job.yml"
if ! grep -q "REPOACH_INSTALL: \"repoach==$expect\"" "$work/job.yml" ; then
    echo "wheel_smoke: the generated job does not pin repoach==$expect:" >&2
    grep -n "REPOACH_INSTALL:" "$work/job.yml" >&2 || true
    exit 1
fi
grep -n "REPOACH_INSTALL:" "$work/job.yml"

echo "wheel_smoke: repoach $expect on $("$python_bin" --version 2>&1): ok"
