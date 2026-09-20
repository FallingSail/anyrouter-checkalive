#!/usr/bin/env bash
# monitor-recovery.sh - Poll all tokens every 30min, send round summary, early-exit when fast
# Designed for manual-trigger GitHub Actions workflow (6h container).
# Reuses keepalive.sh for health checks.
# Usage: monitor-recovery.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# --- Configuration ---
BASE_URL="${BASE_URL:-}"
MODEL="${MODEL:-}"
PROBE_TIMEOUT_SEC="${PROBE_TIMEOUT_SEC:-120}"
SLEEP_BETWEEN_TOKENS="${SLEEP_BETWEEN_TOKENS:-30}"
POLL_INTERVAL="${POLL_INTERVAL:-1800}"          # 30 minutes between rounds
MAX_DURATION_SEC="${MAX_DURATION_SEC:-21500}"   # ~5h58m (just under 6h)
QQ_EMAIL="${QQ_EMAIL:-}"
QQ_SMTP_AUTH_CODE="${QQ_SMTP_AUTH_CODE:-}"

# Beijing time helper
beijing_ts() {
    TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST'
}

# --- Load tokens (reused from run-all.sh) ---

# --- Send email alert (reused from run-all.sh) ---
send_email() {
    local subject="$1" body="$2"
    if [ -z "$QQ_EMAIL" ] || [ -z "$QQ_SMTP_AUTH_CODE" ]; then
        echo "  (Skipping email: QQ_EMAIL or QQ_SMTP_AUTH_CODE not configured)"
        return 0
    fi
    if ! curl --version 2>/dev/null | grep -qi "smtp"; then
        echo "  Email failed: curl was not compiled with SMTP support"
        return 1
    fi
    local mail_file
    mail_file=$(mktemp)
    cat > "$mail_file" <<EOF
From: $QQ_EMAIL
To: $QQ_EMAIL
Subject: $subject
Content-Type: text/plain; charset=utf-8

$body
EOF
    echo "  Sending email via QQ SMTP to $QQ_EMAIL ..."
    local curl_exit=0
    curl -sS --ssl-reqd --fail-with-body --connect-timeout 10 --max-time 30 \
        --url "smtps://smtp.qq.com:465" \
        --user "$QQ_EMAIL:$QQ_SMTP_AUTH_CODE" \
        --login-options "AUTH=LOGIN" \
        --mail-from "$QQ_EMAIL" \
        --mail-rcpt "$QQ_EMAIL" \
        --upload-file "$mail_file" \
        || curl_exit=$?
    rm -f "$mail_file"
    if [ "$curl_exit" -eq 0 ]; then
        echo "  Email sent to $QQ_EMAIL"
        return 0
    else
        echo "  Email FAILED (curl exit: $curl_exit)"
        return 1
    fi
}

# --- Main ---
TOKENS_DATA=$(load_tokens)
validate_settings
mapfile -t TOKENS <<< "$TOKENS_DATA"
if [ ${#TOKENS[@]} -eq 0 ]; then
    echo "ERROR: No tokens loaded. Exiting." >&2
    exit 1
fi
echo "Loaded ${#TOKENS[@]} token(s)"
echo "Model: $MODEL"
echo "Poll interval: ${POLL_INTERVAL}s"
echo ""

PREV_STATES=()
PAUSED=()
MONITOR_EXIT=1

START_TIME=$(date +%s)
ROUND=1

while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    REMAINING=$((MAX_DURATION_SEC - ELAPSED))

    if [ "$REMAINING" -le 0 ]; then
        echo "=== Time limit reached. Exiting. ==="
        break
    fi

    echo "============================================="
    echo " Round $ROUND  |  $(beijing_ts)"
    echo " Elapsed: ${ELAPSED}s  |  Remaining: ~${REMAINING}s"
    echo "============================================="

    # Per-round tracking
    TOKEN_RESULTS=()
    TOKEN_TIMES=()       # response time (seconds), 0 for failed
    ALL_SUCCESS=true
    MAX_TIME=0

    for i in "${!TOKENS[@]}"; do
        TOKEN_RESULTS[$i]="- account-$((i+1)) not checked"
        PREV_STATES[$i]="failed"
    done

    for i in "${!TOKENS[@]}"; do
        token="${TOKENS[$i]}"
        token_preview="account-$((i+1))"

        if [ "${PAUSED[$i]:-false}" = true ]; then
            TOKEN_RESULTS[$i]="- $token_preview config_error (paused)"
            ALL_SUCCESS=false
            continue
        fi

        # Check remaining time before each token
        NOW=$(date +%s)
        if [ $((NOW - START_TIME)) -ge "$MAX_DURATION_SEC" ]; then
            echo "Time limit reached mid-round. Breaking."
            ALL_SUCCESS=false
            break
        fi

        echo "[$((i+1))/${#TOKENS[@]}] Testing $token_preview ..."

        CHECK_START=$(date +%s)
        CHECK_TIMEOUT=$((MAX_DURATION_SEC - NOW + START_TIME))
        [ "$CHECK_TIMEOUT" -gt "$PROBE_TIMEOUT_SEC" ] && CHECK_TIMEOUT=$PROBE_TIMEOUT_SEC
        if result=$(KEEPALIVE_TOKEN="$token" BASE_URL="$BASE_URL" MODEL="$MODEL" PROBE_TIMEOUT_SEC="$CHECK_TIMEOUT" bash "$SCRIPT_DIR/keepalive.sh" 2>&1); then
            CHECK_END=$(date +%s)
            response_time=$((CHECK_END - CHECK_START))
            echo "$result"
            echo "  ✓ $token_preview active (${response_time}s)"

            TOKEN_RESULTS[$i]="✓ $token_preview (${response_time}s)"
            TOKEN_TIMES+=("$response_time")
            PREV_STATES[$i]="success"
            [ "$response_time" -gt "$MAX_TIME" ] && MAX_TIME=$response_time
        else
            probe_exit=$?
            echo "$result"
            echo "  ✗ $token_preview failed"
            TOKEN_RESULTS[$i]="✗ $token_preview failed (probe exit $probe_exit)"
            TOKEN_TIMES+=("0")
            PREV_STATES[$i]="failed"
            ALL_SUCCESS=false
            if [ "$probe_exit" -eq 2 ]; then
                PAUSED[$i]=true
                echo "  Configuration error: pausing $token_preview for this run."
            fi
        fi

        # Brief pause between tokens
        if [ "$i" -lt "$(( ${#TOKENS[@]} - 1 ))" ] && [ "$SLEEP_BETWEEN_TOKENS" -gt 0 ]; then
            JITTER=$(( SLEEP_BETWEEN_TOKENS + (RANDOM % 21) - 10 ))
            [ "$JITTER" -lt 10 ] && JITTER=10
            echo "  Waiting ${JITTER}s ..."
            sleep_with_deadline "$JITTER"
        fi
    done

    # --- End of round: build summary ---
    ROUND_SUMMARY=""
    SUCCESS_COUNT=0
    FAIL_COUNT=0

    for i in "${!TOKENS[@]}"; do
        ROUND_SUMMARY+="  ${TOKEN_RESULTS[$i]}"$'\n'
        if [ "${PREV_STATES[$i]}" = "success" ]; then
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    done

    ROUND_SUMMARY+=$'\n'"Summary: $SUCCESS_COUNT success, $FAIL_COUNT failed"

    echo ""
    echo "--- Round $ROUND summary: $SUCCESS_COUNT success, $FAIL_COUNT failed ---"
    echo ""

    # --- Decide action ---
    if [ "$ALL_SUCCESS" = true ] && [ "$MAX_TIME" -lt 30 ]; then
        # All healthy and fast — early exit
        echo ">>> All tokens healthy (max response ${MAX_TIME}s < 30s). Sending '快用' email and exiting."
        send_email \
            "快用！现在状态超好，不接着测了" \
            "Anyrouter 已全面恢复，响应极快，建议立即使用！

$(beijing_ts)

各 token 状态：
$ROUND_SUMMARY

最大响应时间: ${MAX_TIME}s
所有 token 均正常工作且响应时间 < 30 秒，状态超好！检测到此结束。" || true
        echo ""
        echo "=== Early exit: all healthy and fast. ==="
        MONITOR_EXIT=0
        break
    fi

    # Send normal round summary
    if [ "$ALL_SUCCESS" = true ]; then
        send_email \
            "Anyrouter 监控报告 - 第${ROUND}轮（全部可用）" \
            "轮次: 第 ${ROUND} 轮
检测时间: $(beijing_ts)

各 token 状态：
$ROUND_SUMMARY

最大响应时间: ${MAX_TIME}s
全部可用，但响应时间未达到 30 秒以内的超优标准，继续监控。" || true
    else
        send_email \
            "Anyrouter 监控报告 - 第${ROUND}轮（${FAIL_COUNT}个不可用）" \
            "轮次: 第 ${ROUND} 轮
检测时间: $(beijing_ts)

各 token 状态：
$ROUND_SUMMARY

仍有 ${FAIL_COUNT} 个 token 不可用，继续监控。" || true
    fi

    ROUND=$((ROUND + 1))

    if [ "${#PAUSED[@]}" -eq "${#TOKENS[@]}" ]; then
        echo "All accounts paused due to configuration errors."
        break
    fi

    # --- Sleep until next poll ---
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    REMAINING=$((MAX_DURATION_SEC - ELAPSED))

    if [ "$REMAINING" -gt "$POLL_INTERVAL" ]; then
        echo ""
        echo "--- Next round in ${POLL_INTERVAL}s ($((POLL_INTERVAL / 60)) min) ---"
        sleep_with_deadline "$POLL_INTERVAL"
    elif [ "$REMAINING" -gt 60 ]; then
        echo ""
        echo "--- Time nearly up, sleeping final ${REMAINING}s ---"
        sleep_with_deadline "$REMAINING"
    else
        echo "Time limit reached."
        break
    fi
done

echo ""
echo "========================================"
echo " Monitor completed."
echo " Total rounds: $ROUND"
echo "========================================"
exit "$MONITOR_EXIT"
