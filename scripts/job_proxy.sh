#!/usr/bin/env bash
# SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB: start or stop the LLM proxy that
# belongs to ONE job, on a port chosen at start, and prove that the
# proxy answering /health is that job's own process.
#
#   scripts/job_proxy.sh start   pick a free port and an instance token,
#                                start python -m repoach.llm_proxy, poll
#                                /health until it echoes the token
#   scripts/job_proxy.sh stop    kill the proxy recorded by start
#
# start exports REPOACH_PROXY_PORT, REPOACH_PROXY_INSTANCE and
# REPOACH_LLM_PROXY_BASE_URL to "$GITHUB_ENV" when it is set (every
# later step of the job reads them), and always prints them as
# KEY=VALUE lines on stdout for local callers.
#
# Exit codes of start: 0 own proxy healthy; 1 never became healthy or
# exited before ready; 3 a FOREIGN proxy answered on the chosen port.
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

new_instance() {
    local rand
    rand="$("$py" -c 'import secrets; print(secrets.token_hex(8))')"
    echo "${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${GITHUB_JOB:-job}-$$-${rand}"
}

start() {
    mkdir -p "$dir"
    local port instance base_url pid rc message deadline
    port="$("$py" -m repoach.llm_proxy.instance port --host "$host")"
    instance="$(new_instance)"
    base_url="http://${host}:${port}"

    REPOACH_PROXY_HOST="$host" REPOACH_PROXY_PORT="$port" \
        REPOACH_PROXY_INSTANCE="$instance" \
        nohup "$py" -m repoach.llm_proxy > "$log" 2>&1 &
    pid=$!
    echo "$pid" > "$pidfile"

    deadline=$(( SECONDS + timeout_s ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        set +e
        message="$("$py" -m repoach.llm_proxy.instance check \
            --url "${base_url}/health" --expect "$instance")"
        rc=$?
        set -e
        if [ "$rc" -eq 0 ]; then
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
            echo "job proxy ready: ${message} (pid ${pid})" >&2
            return 0
        fi
        if [ "$rc" -eq 3 ]; then
            err "${message} — the job's own proxy (pid ${pid}) did not get ${host}:${port}"
            kill "$pid" 2>/dev/null || true
            tail -50 "$log" >&2 || true
            return 3
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            err "job proxy (pid ${pid}) exited before answering on ${host}:${port}"
            tail -50 "$log" >&2 || true
            return 1
        fi
        sleep 0.5
    done
    err "job proxy did not become healthy on ${host}:${port} in ${timeout_s}s"
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
    *)
        echo "usage: $0 start|stop" >&2
        exit 2
        ;;
esac
