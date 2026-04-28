#!/usr/bin/env bash
# Hermes Gateway Watchdog — überwacht Gateway-Prozess, Error-Logs und Cron-Jobs.
# Bei kritischen Problemen: Auto-Restart und Telegram-Alert.
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Config
LAUNCHD_LABEL="${LAUNCHD_LABEL:-ai.hermes.gateway}"
ERROR_LOG="${ERROR_LOG:-$HOME/.hermes/logs/gateway.error.log}"
GATEWAY_LOG="${GATEWAY_LOG:-$HOME/.hermes/logs/gateway.log}"
STATE_FILE="${STATE_FILE:-$HOME/.openclaw/workspace/memory/ops/hermes-gateway-watchdog.json}"
MAIN_LOG="${MAIN_LOG:-$HOME/Logs/hermes-gateway-watchdog.log}"
SAFE_SEND="${SAFE_SEND:-$HOME/.openclaw/workspace/scripts/safe_telegram_send.sh}"
GUARD_TO_TASK="${GUARD_TO_TASK:-$HOME/.openclaw/workspace/scripts/guard_to_task.sh}"
PAPERCLIP_WAKE_SCRIPT="${PAPERCLIP_WAKE_SCRIPT:-$HOME/.openclaw/workspace/scripts/paperclip_wakeup_retry.sh}"
PAPERCLIP_AGENT_ID="${PAPERCLIP_AGENT_ID:-3f169b0b-f743-4ce1-9127-b6c8de214fd4}" # Monitoring & Alerting Ops
TELEGRAM_TARGET="${TELEGRAM_TARGET:-128314698}"

# Thresholds
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"                    # 2x = 2min bei 60s Intervall
RESTART_COOLDOWN_SEC="${RESTART_COOLDOWN_SEC:-300}"      # 5min zwischen Restarts
MAX_RESTARTS_WINDOW="${MAX_RESTARTS_WINDOW:-1800}"       # 30min Window
MAX_RESTARTS_COUNT="${MAX_RESTARTS_COUNT:-3}"            # Max 3 Restarts in 30min
ERROR_RATE_THRESHOLD="${ERROR_RATE_THRESHOLD:-5}"        # 5 ERRORs/min
ALERT_AFTER_RESTARTS="${ALERT_AFTER_RESTARTS:-2}"        # Alert nach 2 Restarts
ESCALATE_AFTER_RESTARTS="${ESCALATE_AFTER_RESTARTS:-3}"  # Task nach 3 Restarts

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$MAIN_LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$MAIN_LOG"; }
read_state() { [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo '{}'; }
write_state() { printf '%s\n' "$1" > "$STATE_FILE"; }

safe_jq() {
  local filter="$1"
  local input="$2"
  jq -r "$filter" <<< "$input" 2>/dev/null || echo ""
}

send_telegram() {
  local message="$1"
  local context="${2:-hermes_gateway_watchdog}"
  local dedupe_key="${3:-}"

  if [[ ! -x "$SAFE_SEND" ]]; then
    log "safe_telegram_send not found, skipping alert"
    return 1
  fi

  local args=(
    --target "$TELEGRAM_TARGET"
    --context "$context"
    --message "$message"
  )

  if [[ -n "$dedupe_key" ]]; then
    args+=(--dedupe-key "$dedupe_key" --dedupe-window 300)
  fi

  "$SAFE_SEND" "${args[@]}" >/dev/null 2>&1 || return 1
  return 0
}

escalate_to_task() {
  local reason="$1"
  local restart_count="$2"

  [[ -x "$GUARD_TO_TASK" ]] || return 1

  local title="Guard: Hermes Gateway instabil"
  local desc="Automatisch erstellt vom Hermes-Gateway-Watchdog.\n\nReason: $reason\nAuto-Restarts: $restart_count\n\nBitte Root Cause analysieren:\n- Telegram-API-Probleme\n- Provider-Rate-Limits\n- Cron-Job-Timeouts\n- Resource-Exhaustion"

  "$GUARD_TO_TASK" \
    --project techops-marco \
    --title "$title" \
    --description "$desc" \
    --priority high \
    --guard-name hermes_gateway_watchdog >/dev/null 2>&1 || return 1

  # Wake Monitoring & Alerting Ops Agent
  if [[ -n "$PAPERCLIP_AGENT_ID" && -x "$PAPERCLIP_WAKE_SCRIPT" ]]; then
    "$PAPERCLIP_WAKE_SCRIPT" "$PAPERCLIP_AGENT_ID" on_demand >/dev/null 2>&1 || true
  fi

  return 0
}

check_process_running() {
  local uid
  uid="$(id -u)"
  launchctl list | grep -q "$LAUNCHD_LABEL"
}

count_recent_errors() {
  [[ -f "$ERROR_LOG" ]] || { echo "0"; return; }

  # Zähle ERROR-Zeilen der letzten 60s
  local now_ts cutoff_ts
  now_ts=$(date +%s)
  cutoff_ts=$(( now_ts - 60 ))

  # Annahme: Log-Format enthält Timestamps — wenn nicht, nehme einfach letzte Zeilen
  local error_count
  error_count=$(tail -200 "$ERROR_LOG" 2>/dev/null | grep -c "^ERROR")
  echo "$error_count"
}

check_cron_jobs() {
  # Prüfe ob Cron-Jobs im Error-Log als "failed" erscheinen
  [[ -f "$ERROR_LOG" ]] || return 0

  local recent_failures
  recent_failures=$(tail -100 "$ERROR_LOG" 2>/dev/null | grep -c "cron.scheduler.*failed")

  if (( recent_failures > 0 )); then
    log "detected $recent_failures cron job failures in recent logs"
    return 1
  fi
  return 0
}

# Main Check Logic
now_s=$(date +%s)
now_ms=$(( now_s * 1000 ))

state="$(read_state)"
cfail="$(safe_jq '.consecutive_failures // 0' "$state")"; [[ -z "$cfail" ]] && cfail=0
last_restart="$(safe_jq '.last_restart_at_ms // 0' "$state")"; [[ -z "$last_restart" ]] && last_restart=0
restart_count="$(safe_jq '.restart_count // 0' "$state")"; [[ -z "$restart_count" ]] && restart_count=0
restart_history="$(safe_jq '.restart_history // []' "$state")"; [[ -z "$restart_history" ]] && restart_history="[]"
alerted="$(safe_jq '.alerted // false' "$state")"; [[ -z "$alerted" ]] && alerted=false
escalated="$(safe_jq '.escalated // false' "$state")"; [[ -z "$escalated" ]] && escalated=false

# Cleanup alte Restart-History (außerhalb 30min Window)
window_start=$(( now_ms - MAX_RESTARTS_WINDOW * 1000 ))
restart_history=$(jq -c "[.[] | select(. > $window_start)]" <<< "$restart_history" 2>/dev/null || echo "[]")
restart_count=$(jq 'length' <<< "$restart_history" 2>/dev/null || echo "0")

# Check 1: Prozess läuft?
process_healthy=true
if ! check_process_running; then
  log "gateway process not running"
  process_healthy=false
fi

# Check 2: Error-Rate
error_count=$(count_recent_errors)
error_rate_critical=false
if (( error_count >= ERROR_RATE_THRESHOLD )); then
  log "high error rate detected: $error_count errors/min"
  error_rate_critical=true
  process_healthy=false
fi

# Check 3: Cron-Job-Health
cron_healthy=true
if ! check_cron_jobs; then
  cron_healthy=false
  process_healthy=false
fi

# Healthy: Reset State
if $process_healthy; then
  if (( cfail > 0 )) || [[ "$alerted" == "true" ]]; then
    log "gateway recovered after $cfail failed checks"

    # Recovery-Notification nur wenn zuvor Alert gesendet
    if [[ "$alerted" == "true" ]]; then
      send_telegram "✅ Hermes Gateway wieder stabil\n\nNach $restart_count Auto-Restarts ist das Gateway wieder healthy." \
        "hermes_recovery" \
        "hermes-recovered:$now_s"
    fi
  fi

  # Reset, aber behalte restart_history für Trend-Analyse
  write_state "$(jq -cn \
    --argjson rh "$restart_history" \
    '{
      consecutive_failures:0,
      last_restart_at_ms:0,
      restart_count:($rh|length),
      restart_history:$rh,
      alerted:false,
      escalated:false
    }')"
  exit 0
fi

# Unhealthy: Increment Failure Counter
cfail=$(( cfail + 1 ))
log "gateway unhealthy (fail $cfail/$FAIL_THRESHOLD)"

# Restart nach FAIL_THRESHOLD
if (( cfail >= FAIL_THRESHOLD )); then
  age_ms=$(( now_ms - last_restart ))

  if (( age_ms >= RESTART_COOLDOWN_SEC * 1000 )); then
    log "restarting gateway (fails=$cfail, restarts_in_window=$restart_count)"

    uid="$(id -u)"
    restart_out="$(launchctl kickstart -k "gui/$uid/$LAUNCHD_LABEL" 2>&1)"
    restart_rc=$?
    [[ -n "$restart_out" ]] && log "restart output: $restart_out"

    if (( restart_rc == 0 )); then
      last_restart="$now_ms"
      restart_history=$(jq -c ". + [$now_ms]" <<< "$restart_history")
      restart_count=$(jq 'length' <<< "$restart_history")

      # Alert nach N Restarts
      if (( restart_count >= ALERT_AFTER_RESTARTS )) && [[ "$alerted" != "true" ]]; then
        local reason="Gateway instabil"
        if $error_rate_critical; then
          reason="High Error Rate ($error_count errors/min)"
        elif ! $cron_healthy; then
          reason="Cron Job Failures"
        fi

        send_telegram "🚨 Hermes Gateway Probleme\n\nReason: $reason\nAuto-Restarts: $restart_count in ${MAX_RESTARTS_WINDOW}s\n\nIch versuche Recovery, aber bitte System prüfen." \
          "hermes_alert" \
          "hermes-alert:$now_s"
        alerted=true
      fi

      # Escalate nach MAX_RESTARTS
      if (( restart_count >= ESCALATE_AFTER_RESTARTS )) && [[ "$escalated" != "true" ]]; then
        local reason="$restart_count Restarts in ${MAX_RESTARTS_WINDOW}s"
        if escalate_to_task "$reason" "$restart_count"; then
          escalated=true
          log "escalated to guard task after $restart_count restarts"
        fi
      fi
    fi
  else
    log "restart cooldown active (${age_ms}ms < $((RESTART_COOLDOWN_SEC * 1000))ms)"
  fi
fi

write_state "$(jq -cn \
  --argjson cf "$cfail" \
  --argjson lr "$last_restart" \
  --argjson rc "$restart_count" \
  --argjson rh "$restart_history" \
  --arg a "$alerted" \
  --arg e "$escalated" \
  '{
    consecutive_failures:$cf,
    last_restart_at_ms:$lr,
    restart_count:$rc,
    restart_history:$rh,
    alerted:($a=="true"),
    escalated:($e=="true")
  }')"

exit 0
