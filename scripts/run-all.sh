#!/usr/bin/env bash
# run-all.sh - Batch health-check runner with internal 50-minute loop
# Designed for a single GitHub Actions container: runs rounds until ~5h58m time limit.
# Supports local usage via .env file or ANYROUTER_TOKENS env var.
# Usage: run-all.sh [--once]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

# --- Parse args ---
ONCE=false
if [ "${1:-}" = "--once" ]; then
    ONCE=true
fi

# --- Configuration ---
BASE_URL="${BASE_URL:-}"
MODEL="${MODEL:-}"
PROBE_TIMEOUT_SEC="${PROBE_TIMEOUT_SEC:-120}"
SLEEP_BETWEEN_TOKENS="${SLEEP_BETWEEN_TOKENS:-30}"         # seconds between tokens
SLEEP_BETWEEN_ROUNDS="${SLEEP_BETWEEN_ROUNDS:-3000}"       # ~50 minutes between rounds
MAX_DURATION_SEC="${MAX_DURATION_SEC:-21500}"               # ~5h58m (just under 6h limit)
QQ_EMAIL="${QQ_EMAIL:-}"
QQ_SMTP_AUTH_CODE="${QQ_SMTP_AUTH_CODE:-}"

# --- Load tokens ---

# --- Email report ---
send_email() {
    local subject="$1" body="$2"
    if [ -z "$QQ_EMAIL" ] || [ -z "$QQ_SMTP_AUTH_CODE" ]; then
        echo "  (Skipping email: QQ_EMAIL or QQ_SMTP_AUTH_CODE not configured)"
        return 0
    fi

    # Verify curl supports SMTP (GitHub Actions curl usually does)
    if ! curl --version 2>/dev/null | grep -qi "smtp"; then
        echo "  Email failed: curl was not compiled with SMTP support"
        return 1
    fi

    # Write email to temp file (more reliable than here-string + stdin)
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
        echo "  Common causes:"
        echo "    - QQ_SMTP_AUTH_CODE is wrong (it is NOT your QQ password)"
        echo "    - Generate it at: QQ Mail -> Settings -> Account -> POP3/IMAP/SMTP"
        echo "    - Network/firewall blocking smtps://smtp.qq.com:465"
        return 1
    fi
}

# --- Load tokens ---
TOKENS_DATA=$(load_tokens)
validate_settings
mapfile -t TOKENS <<< "$TOKENS_DATA"
if [ ${#TOKENS[@]} -eq 0 ]; then
    echo "ERROR: No tokens loaded. Exiting." >&2
    exit 1
fi
echo "Loaded ${#TOKENS[@]} token(s)"
echo "Model: $MODEL"
echo ""

START_TIME=$(date +%s)
ROUND=1
ALL_RESULTS=""
HAS_SENT_REPORT=false
PAUSED=()
OVERALL_EXIT=0

while true; do
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    REMAINING=$((MAX_DURATION_SEC - ELAPSED))

    if [ "$REMAINING" -le 0 ]; then
        echo "=== Time limit reached. Exiting. ==="
        break
    fi

    echo "========================================"
    echo " Round $ROUND  |  $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo " Elapsed: ${ELAPSED}s  |  Remaining: ~${REMAINING}s"
    echo "========================================"

    ROUND_RESULTS=""
    ROUND_SUCCESS=0
    ROUND_FAIL=0

    for i in "${!TOKENS[@]}"; do
        token="${TOKENS[$i]}"
        token_preview="account-$((i+1))"

        if [ "${PAUSED[$i]:-false}" = true ]; then
            ROUND_RESULTS+="  - $token_preview config_error (paused)"$'\n'
            ROUND_FAIL=$((ROUND_FAIL + 1))
            continue
        fi

        # Check remaining time before each token
        NOW=$(date +%s)
        if [ $((NOW - START_TIME)) -ge "$MAX_DURATION_SEC" ]; then
            echo "Time limit reached mid-round. Breaking."
            OVERALL_EXIT=1
            break
        fi

        echo "[$((i+1))/${#TOKENS[@]}] Testing $token_preview ..."

        CHECK_TIMEOUT=$((MAX_DURATION_SEC - NOW + START_TIME))
        [ "$CHECK_TIMEOUT" -gt "$PROBE_TIMEOUT_SEC" ] && CHECK_TIMEOUT=$PROBE_TIMEOUT_SEC
        if result=$(KEEPALIVE_TOKEN="$token" BASE_URL="$BASE_URL" MODEL="$MODEL" PROBE_TIMEOUT_SEC="$CHECK_TIMEOUT" bash "$SCRIPT_DIR/keepalive.sh" 2>&1); then
            echo "$result"
            echo "  ✓ $token_preview is active"
            ROUND_RESULTS+="  ✓ $token_preview is active"$'\n'
            ROUND_SUCCESS=$((ROUND_SUCCESS + 1))
        else
            probe_exit=$?
            echo "$result"
            echo "  ✗ $token_preview failed"
            ROUND_RESULTS+="  ✗ $token_preview failed (probe exit $probe_exit)"$'\n'
            ROUND_FAIL=$((ROUND_FAIL + 1))
            OVERALL_EXIT=1
            if [ "$probe_exit" -eq 2 ]; then
                PAUSED[$i]=true
                echo "  Configuration error: pausing $token_preview for this run."
            fi
        fi

        # Add random jitter to interval (20-40s instead of fixed 30s)
        if [ "$i" -lt "$(( ${#TOKENS[@]} - 1 ))" ] && [ "$SLEEP_BETWEEN_TOKENS" -gt 0 ]; then
            JITTER=$(( SLEEP_BETWEEN_TOKENS + (RANDOM % 21) - 10 ))
            [ "$JITTER" -lt 10 ] && JITTER=10
            echo "  Waiting ${JITTER}s ..."
            sleep_with_deadline "$JITTER"
        fi
    done

    # Accumulate round results
    ALL_RESULTS+="--- Round $ROUND ($(date '+%Y-%m-%d %H:%M')) ---"$'\n'
    ALL_RESULTS+="$ROUND_RESULTS"$'\n'
    ALL_RESULTS+="Round $ROUND summary: $ROUND_SUCCESS success, $ROUND_FAIL failed"$'\n'$'\n'

    echo ""
    echo "--- Round $ROUND summary: $ROUND_SUCCESS success, $ROUND_FAIL failed ---"

    ROUND=$((ROUND + 1))

    if [ "${#PAUSED[@]}" -eq "${#TOKENS[@]}" ]; then
        echo "All accounts paused due to configuration errors."
        break
    fi

    # If --once mode, exit after the first round
    if [ "$ONCE" = true ]; then
        echo ""
        echo "=== --once mode: single round complete. Exiting. ==="
        break
    fi

    # Check if we should send final report (last round before time limit)
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    REMAINING=$((MAX_DURATION_SEC - ELAPSED))

    if [ "$REMAINING" -le "$((SLEEP_BETWEEN_ROUNDS + 120))" ] && [ "$HAS_SENT_REPORT" = false ]; then
        HAS_SENT_REPORT=true
        echo ""
        echo "=== Sending final report ==="
        send_email "Anyrouter Keepalive Report ($(date '+%Y-%m-%d'))" "$ALL_RESULTS" || true
        echo ""

        # Do one more round if time allows, but signal it's the last
        if [ "$REMAINING" -le 0 ]; then
            break
        fi
    fi

    # Sleep until next round (if we have time)
    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TIME))
    REMAINING=$((MAX_DURATION_SEC - ELAPSED))

    if [ "$REMAINING" -gt "$SLEEP_BETWEEN_ROUNDS" ]; then
        echo "Sleeping ${SLEEP_BETWEEN_ROUNDS}s until round $ROUND ..."
        sleep_with_deadline "$SLEEP_BETWEEN_ROUNDS"
    elif [ "$REMAINING" -gt 60 ]; then
        echo "Sleeping ${REMAINING}s (remaining time) ..."
        sleep_with_deadline "$REMAINING"
    else
        echo "Time limit reached."
        break
    fi
done

# Final summary
echo ""
echo "========================================"
echo " All rounds complete."
echo "$ALL_RESULTS"
echo "========================================"

# Send one final report if we never sent one (e.g. very short run)
if [ "$HAS_SENT_REPORT" = false ]; then
    send_email "Anyrouter Keepalive Report ($(date '+%Y-%m-%d'))" "$ALL_RESULTS" || true
fi

echo "Done."
exit "$OVERALL_EXIT"
