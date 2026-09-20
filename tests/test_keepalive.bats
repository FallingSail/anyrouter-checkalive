setup() {
    cd "$BATS_TEST_DIRNAME/.."
    export TEST_DIR="$(mktemp -d)"
    export BASE_URL="https://relay.example/v1"
    export MODEL="test-model"
    export ANYROUTER_TOKENS="test-token"
    export SLEEP_BETWEEN_TOKENS=0
    export MAX_DURATION_SEC=10
    export QQ_EMAIL=""
    export QQ_SMTP_AUTH_CODE=""
    export PYTHON_BIN="$TEST_DIR/mock-python"
    cat > "$PYTHON_BIN" <<'MOCK'
#!/usr/bin/env bash
if [ "${MOCK_STATUS:-success}" = config_error ]; then
    echo "  STATUS=config_error"
    exit 2
fi
echo "  STATUS=success"
MOCK
    chmod +x "$PYTHON_BIN"
}

teardown() {
    rm -rf "$TEST_DIR"
}

@test "batch fails without tokens" {
    unset ANYROUTER_TOKENS
    run bash scripts/run-all.sh --once
    [ "$status" -ne 0 ]
    [[ "$output" == *"No tokens"* ]]
}

@test "launcher falls back from unavailable python3 to python" {
    unset PYTHON_BIN
    mkdir -p "$TEST_DIR/bin"
    ln -s "$(command -v dirname)" "$TEST_DIR/bin/dirname"
    printf '#!/bin/sh\nexit 127\n' > "$TEST_DIR/bin/python3"
    printf '#!/bin/sh\nif [ "$1" = -c ]; then exit 0; fi\nprintf "python fallback selected\\n"\n' > "$TEST_DIR/bin/python"
    chmod +x "$TEST_DIR/bin/python3" "$TEST_DIR/bin/python"
    local bash_bin
    bash_bin="$(command -v bash)"
    run env PATH="$TEST_DIR/bin" "$bash_bin" scripts/keepalive.sh
    [ "$status" -eq 0 ]
    [[ "$output" == *"python fallback selected"* ]]
}

@test "batch requires explicit relay settings" {
    unset BASE_URL
    run bash scripts/run-all.sh --once
    [ "$status" -eq 2 ]
}

@test "tokens are trimmed and deduplicated" {
    export ANYROUTER_TOKENS=$'test-token\r\n\n test-token \nsecond-token'
    run bash scripts/run-all.sh --once
    [ "$status" -eq 0 ]
    [[ "$output" == *"Loaded 2 token(s)"* ]]
    [[ "$output" != *"test-token"* ]]
}

@test "successful single round exits zero" {
    run bash scripts/run-all.sh --once
    [ "$status" -eq 0 ]
    [[ "$output" == *"1 success, 0 failed"* ]]
}

@test "configuration errors pause accounts and fail batch" {
    export MOCK_STATUS=config_error
    run bash scripts/run-all.sh
    [ "$status" -eq 1 ]
    [[ "$output" == *"All accounts paused"* ]]
}

@test "monitor succeeds when every account recovers" {
    run bash scripts/monitor-recovery.sh
    [ "$status" -eq 0 ]
    [[ "$output" == *"Early exit"* ]]
}

@test "monitor stops when all accounts have configuration errors" {
    export MOCK_STATUS=config_error
    run bash scripts/monitor-recovery.sh
    [ "$status" -eq 1 ]
    [[ "$output" == *"All accounts paused"* ]]
}
