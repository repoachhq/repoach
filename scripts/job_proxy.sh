#!/usr/bin/env bash
# SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB: start or stop the LLM proxy that
# belongs to ONE job, on a port chosen at start, and prove that the
# process listening on that port is the one this job spawned.
#
#   job_proxy.sh start                     pick a free port and an instance
#                                          token, spawn the proxy, wait for
#                                          the ownership proof
#   job_proxy.sh stop                      kill the proxy recorded by start
#   job_proxy.sh prove PID PORT INSTANCE   one ownership probe (exit codes
#                                          below), used by start and tests
#
# The proof never relies on the proxy's own code, so it holds when the
# proxy comes from an older base ref:
#   1. PID proof (decides): the LISTEN socket on PORT, read from
#      /proc/net/tcp{,6}, has an inode that appears among PID's
#      /proc/PID/fd links. Both are readable without root for a process
#      of the same user.
#   2. Token proof (second, when available): /health echoes `instance`;
#      it must equal INSTANCE. A proxy without the echo is accepted on
#      the PID proof alone and the helper says so.
#
# prove exit codes: 0 own proxy; 1 not ready yet (no listener, or
# /health not healthy yet); 3 foreign proxy (listener not owned by PID,
# or another instance token); 4 the spawned PID is dead.
#
# start exports REPOACH_PROXY_PORT, REPOACH_PROXY_INSTANCE and
# REPOACH_LLM_PROXY_BASE_URL to "$GITHUB_ENV" when it is set, prints them
# as KEY=VALUE lines on stdout, and exits 0, 1 (timeout or spawned proxy
# dead) or 3 (foreign proxy).
#
# The script uses bash, coreutils, awk, curl and the Python standard
# library only; it imports nothing from repoach.
#
# Tunables (env): JOB_PROXY_HOST (127.0.0.1), JOB_PROXY_DIR (logs),
# JOB_PROXY_TIMEOUT_S (20), PYTHON (python).
set -euo pipefail

host="${JOB_PROXY_HOST:-127.0.0.1}"
dir="${JOB_PROXY_DIR:-logs}"
timeout_s="${JOB_PROXY_TIMEOUT_S:-20}"
py="${PYTHON:-python}"
log="$dir/llm_proxy.log"
pidfile="$dir/llm_proxy.pid"

err() {
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::error::$*" >&2
    else
        echo "error: $*" >&2
    fi
}

listen_inodes() {
    local hexport
    hexport="$(printf '%04X' "$1")"
    { cat /proc/net/tcp; cat /proc/net/tcp6 2>/dev/null || true; } \
        | awk -v p=":${hexport}" '$4 == "0A" && substr($2, length($2) - 4) == p { print $10 }'
}

pid_owns_inode() {
    local pid="$1" inode="$2" fd
    for fd in /proc/"$pid"/fd/*; do
        if [ "$(readlink "$fd" 2>/dev/null)" = "socket:[${inode}]" ]; then
            return 0
        fi
    done
    return 1
}

health_field() {
    "$py" -c 'import json, sys
try:
    body = json.load(sys.stdin)
except ValueError:
    body = None
if not isinstance(body, dict):
    body = {}
print(body.get(sys.argv[1]) or "")' "$1"
}

prove() {
    local pid="$1" port="$2" instance="$3" inodes inode owned=0 body status seen
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "spawned proxy (pid ${pid}) is dead — nothing of ours listens on ${host}:${port}"
        return 4
    fi
    inodes="$(listen_inodes "$port")"
    if [ -z "$inodes" ]; then
        echo "no listener on ${host}:${port} yet"
        return 1
    fi
    for inode in $inodes; do
        if pid_owns_inode "$pid" "$inode"; then
            owned=1
        fi
    done
    if [ "$owned" -ne 1 ]; then
        echo "foreign proxy on ${host}:${port} (listener not owned by pid ${pid})"
        return 3
    fi
    body="$(curl --silent --max-time 1 "http://${host}:${port}/health" || true)"
    status="$(printf '%s' "$body" | health_field status)"
    if [ "$status" != "healthy" ]; then
        echo "own listener on ${host}:${port} (pid ${pid}), /health not healthy yet"
        return 1
    fi
    seen="$(printf '%s' "$body" | health_field instance)"
    if [ -z "$seen" ]; then
        echo "own proxy on ${host}:${port} (pid ${pid}); instance echo absent (proxy predates SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB)"
        return 0
    fi
    if [ "$seen" != "$instance" ]; then
        echo "foreign proxy on ${host}:${port} (instance=${seen})"
        return 3
    fi
    echo "own proxy on ${host}:${port} (pid ${pid}, instance ${instance})"
    return 0
}

start() {
    mkdir -p "$dir"
    local port instance base_url pid rc message deadline rand
    port="$("$py" -c 'import socket, sys
s = socket.socket()
s.bind((sys.argv[1], 0))
print(s.getsockname()[1])
s.close()' "$host")"
    rand="$("$py" -c 'import secrets; print(secrets.token_hex(8))')"
    instance="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${GITHUB_JOB:-job}-$$-${rand}"
    base_url="http://${host}:${port}"

    REPOACH_PROXY_HOST="$host" REPOACH_PROXY_PORT="$port" \
        REPOACH_PROXY_INSTANCE="$instance" \
        nohup "$py" -m repoach.llm_proxy > "$log" 2>&1 &
    pid=$!
    echo "$pid" > "$pidfile"

    deadline=$(( SECONDS + timeout_s ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        set +e
        message="$(prove "$pid" "$port" "$instance")"
        rc=$?
        set -e
        case "$rc" in
            0)
                if [ -n "${GITHUB_ENV:-}" ]; then
                    {
                        echo "REPOACH_PROXY_PORT=${port}"
                        echo "REPOACH_PROXY_INSTANCE=${instance}"
                        echo "REPOACH_LLM_PROXY_BASE_URL=${base_url}"
                    } >> "$GITHUB_ENV"
                fi
                echo "REPOACH_PROXY_PORT=${port}"
                echo "REPOACH_PROXY_INSTANCE=${instance}"
                echo "REPOACH_LLM_PROXY_BASE_URL=${base_url}"
                echo "job proxy ready: ${message}" >&2
                return 0
                ;;
            3)
                err "$message"
                kill "$pid" 2>/dev/null || true
                tail -50 "$log" >&2 || true
                return 3
                ;;
            4)
                err "$message"
                tail -50 "$log" >&2 || true
                return 1
                ;;
        esac
        sleep 0.5
    done
    err "job proxy did not become healthy on ${host}:${port} in ${timeout_s}s (${message})"
    kill "$pid" 2>/dev/null || true
    tail -50 "$log" >&2 || true
    return 1
}

stop() {
    if [ ! -f "$pidfile" ]; then
        echo "no job proxy pid file at ${pidfile} — nothing to stop" >&2
        return 0
    fi
    local pid
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "job proxy (pid ${pid}) stopped" >&2
    else
        echo "job proxy (pid ${pid}) already gone" >&2
    fi
    rm -f "$pidfile"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    prove)
        if [ "$#" -ne 4 ]; then
            echo "usage: $0 prove PID PORT INSTANCE" >&2
            exit 2
        fi
        prove "$2" "$3" "$4"
        ;;
    *)
        echo "usage: $0 start|stop|prove PID PORT INSTANCE" >&2
        exit 2
        ;;
esac
