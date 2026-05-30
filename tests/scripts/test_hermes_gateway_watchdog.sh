#!/usr/bin/env bash
set -euo pipefail

SCRIPT="${SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/hermes_gateway_watchdog.sh}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

export STATE_FILE="$tmp/state.json"
export MAIN_LOG="$tmp/watchdog.log"
export ERROR_LOG="$tmp/gateway.error.log"
export SAFE_SEND="$tmp/missing-safe-send.sh"
export GUARD_TO_TASK="$tmp/missing-guard-to-task.sh"
export PAPERCLIP_WAKE_SCRIPT="$tmp/missing-paperclip-wakeup.sh"
export HERMES_GATEWAY_HEALTH_URL=""

source "$SCRIPT"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_eq() {
  local expected="$1"
  local actual="$2"
  local label="$3"
  [[ "$expected" == "$actual" ]] || fail "$label: expected '$expected', got '$actual'"
}

launchctl() {
  [[ "${1:-}" == "print" ]] || return 1
  printf 'service = enabled\n\tpid = %s\n' "$$"
}
check_process_running || fail "running launchd service with live pid should be healthy"

launchctl() {
  [[ "${1:-}" == "print" ]] || return 1
  printf 'service = enabled\n\tstate = waiting\n'
}
if check_process_running; then
  fail "loaded launchd service without pid should be unhealthy"
fi

printf 'ERROR old failure\nINFO old note\n' > "$ERROR_LOG"
read_log_window '{}'
assert_eq "1" "$(count_new_errors "$LOG_WINDOW_CHUNK")" "first read counts existing error"

state="$(jq -cn \
  --arg inode "$LOG_WINDOW_INODE" \
  --argjson offset "$LOG_WINDOW_OFFSET" \
  '{error_log_inode:$inode,error_log_offset:$offset}')"
read_log_window "$state"
assert_eq "0" "$(count_new_errors "$LOG_WINDOW_CHUNK")" "second read ignores old error"

printf 'INFO fresh note\nERROR fresh failure\ncron.scheduler job failed\n' >> "$ERROR_LOG"
read_log_window "$state"
assert_eq "1" "$(count_new_errors "$LOG_WINDOW_CHUNK")" "new window counts only appended error"
if check_cron_jobs "$LOG_WINDOW_CHUNK"; then
  fail "new cron failure should make cron check unhealthy"
fi

state="$(jq -cn \
  --arg inode "$LOG_WINDOW_INODE" \
  --argjson offset "$LOG_WINDOW_OFFSET" \
  '{error_log_inode:$inode,error_log_offset:$offset}')"
: > "$ERROR_LOG"
read_log_window "$state"
assert_eq "0" "$LOG_WINDOW_OFFSET" "truncated log resets offset"
assert_eq "0" "$(count_new_errors "$LOG_WINDOW_CHUNK")" "truncated empty log has no errors"

echo "hermes_gateway_watchdog tests passed"
