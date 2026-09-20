#!/usr/bin/env bash

load_tokens() {
    local tokens
    tokens=$(printf '%s\n' "${ANYROUTER_TOKENS:-}" | tr -d '\r' | awk 'NF {$1=$1; if (!seen[$0]++) print}')
    if [ -z "$tokens" ]; then
        echo "ERROR: No tokens found. Export ANYROUTER_TOKENS first." >&2
        return 1
    fi
    printf '%s\n' "$tokens"
}

validate_settings() {
    if [ -z "$BASE_URL" ] || [ -z "$MODEL" ]; then
        echo "ERROR: Set BASE_URL and MODEL explicitly for your Codex relay." >&2
        return 2
    fi
    local setting
    for setting in MAX_DURATION_SEC PROBE_TIMEOUT_SEC SLEEP_BETWEEN_ROUNDS POLL_INTERVAL; do
        if [ -n "${!setting:-}" ] && ! [[ "${!setting}" =~ ^[1-9][0-9]*$ ]]; then
            echo "ERROR: $setting must be a positive integer." >&2
            return 2
        fi
    done
    if ! [[ "${SLEEP_BETWEEN_TOKENS:-30}" =~ ^(0|[1-9][0-9]*)$ ]]; then
        echo "ERROR: SLEEP_BETWEEN_TOKENS must be a nonnegative integer." >&2
        return 2
    fi
    if [ "$PROBE_TIMEOUT_SEC" -gt 600 ]; then
        echo "ERROR: PROBE_TIMEOUT_SEC must not exceed 600." >&2
        return 2
    fi
}

sleep_with_deadline() {
    local duration="$1" remaining
    remaining=$((MAX_DURATION_SEC - $(date +%s) + START_TIME))
    [ "$duration" -gt "$remaining" ] && duration=$remaining
    if [ "$duration" -gt 0 ]; then
        sleep "$duration"
    fi
}
