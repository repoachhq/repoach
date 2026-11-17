#!/usr/bin/env bash
# SP-LEDGER-FORMAT-IS-VERSIONED D5: feed a findings ledger written by one
# ref's code to the merge gate reader of another ref.
#
# Usage:
#   scripts/skew_replay.sh <reader-ref> <writer-ref> [--keep]
#
# Two detached worktrees are made under tmp/skew/ of this repository. The
# writer's code writes a stamped ledger holding every enum value it defines
# (python -m repoach.review.ledger_format demo); the reader's code checks it
# (python -m repoach.review.ledger_format check). One line is printed:
#   ledger: ok (format N, writer W, reader R)      exit 0
#   ledger: <the refusal>                          exit 5
# Exit 2 names a ref that does not resolve, a worktree that cannot be made,
# or a ref whose code has no ledger_format module. --keep leaves tmp/skew/.
# PYTHON, when set, names the interpreter (default: python); it must have
# the dependencies of both refs installed.

set -euo pipefail

reader_ref=""
writer_ref=""
keep=0

while [[ $# -gt 0 ]] ; do
    case "$1" in
        --keep) keep=1 ; shift ;;
        -*)     echo "skew_replay: unknown option $1" >&2 ; exit 2 ;;
        *)
            if [[ -z "$reader_ref" ]] ; then
                reader_ref="$1"
            elif [[ -z "$writer_ref" ]] ; then
                writer_ref="$1"
            else
                echo "skew_replay: unexpected argument $1" >&2 ; exit 2
            fi
            shift ;;
    esac
done

if [[ -z "$reader_ref" || -z "$writer_ref" ]] ; then
    echo "usage: $0 <reader-ref> <writer-ref> [--keep]" >&2
    exit 2
fi

python_bin="${PYTHON:-python}"
repo="$(git rev-parse --show-toplevel)"

resolve() {
    git -C "$repo" rev-parse --verify --quiet --end-of-options "$1^{commit}"
}

if ! reader_sha="$(resolve "$reader_ref")" ; then
    echo "skew_replay: reader ref $reader_ref does not resolve" >&2
    exit 2
fi
if ! writer_sha="$(resolve "$writer_ref")" ; then
    echo "skew_replay: writer ref $writer_ref does not resolve" >&2
    exit 2
fi

skew="$repo/tmp/skew"
reader_tree="$skew/reader"
writer_tree="$skew/writer"

if [[ -e "$skew" ]] ; then
    echo "skew_replay: $skew already exists (a kept run?); remove it first" >&2
    exit 2
fi

cleanup_on_exit() {
    local exit_code=$?
    set +e
    if [[ "$keep" -eq 1 ]] ; then
        echo "skew_replay: kept $skew" >&2
        return "$exit_code"
    fi
    for tree in "$reader_tree" "$writer_tree" ; do
        if [[ -d "$tree" ]] ; then
            git -C "$repo" worktree remove --force "$tree" >/dev/null 2>&1 || true
        fi
    done
    git -C "$repo" worktree prune
    rm -rf "$skew"
    return "$exit_code"
}
trap cleanup_on_exit EXIT
mkdir -p "$skew"

for side in reader writer ; do
    if [[ "$side" == reader ]] ; then
        tree="$reader_tree" ; sha="$reader_sha" ; ref="$reader_ref"
    else
        tree="$writer_tree" ; sha="$writer_sha" ; ref="$writer_ref"
    fi
    if ! git -C "$repo" worktree add --quiet --detach "$tree" "$sha" >/dev/null 2>&1 ; then
        echo "skew_replay: cannot make the $side worktree of $ref at $tree" >&2
        exit 2
    fi
    if [[ ! -f "$tree/src/repoach/review/ledger_format.py" ]] ; then
        echo "skew_replay: $side $ref has no ledger_format module (${sha:0:7})" >&2
        exit 2
    fi
done

ledger="$skew/repoach_review.db"
writer_label="$writer_ref (${writer_sha:0:7})"
reader_label="$reader_ref (${reader_sha:0:7})"

if ! (cd "$skew" && PYTHONPATH="$writer_tree/src" "$python_bin" -P -m repoach.review.ledger_format \
        demo --db "$ledger" --written-by "$writer_label" >/dev/null) ; then
    echo "skew_replay: the writer $writer_ref could not write the demo ledger" >&2
    exit 1
fi

set +e
report="$(cd "$skew" && PYTHONPATH="$reader_tree/src" "$python_bin" -P -m repoach.review.ledger_format \
    check --db "$ledger" --pr 0 --reader "$reader_label" 2>"$skew/check.err")"
rc=$?
set -e

case "$rc" in
    0)
        format="$("$python_bin" -I -c 'import json, sys; print(json.loads(sys.argv[1])["format"])' "$report")"
        echo "ledger: ok (format $format, writer $writer_label, reader $reader_label)"
        ;;
    5)
        cat "$skew/check.err"
        ;;
    *)
        echo "skew_replay: the reader $reader_ref failed to check the ledger (exit $rc):" >&2
        cat "$skew/check.err" >&2
        rc=1
        ;;
esac
[[ "$rc" -eq 0 ]] || exit "$rc"
