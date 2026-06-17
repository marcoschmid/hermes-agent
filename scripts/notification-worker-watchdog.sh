#!/bin/bash
# Watchdog für notification_worker LaunchAgent (P4 Track C followup).
#
# Checks:
# 1. LaunchAgent loaded? Else: bootstrap + telegram alert.
# 2. Heartbeat file mtime < HEARTBEAT_STALE_SECONDS old? Else: bootout+bootstrap.
# 3. Pending-row backlog > STAGNATION_THRESHOLD rows older than STAGNATION_AGE?
#    -> alert (worker may be dispatching but cannot keep up, OR Hub/MC down).
#
# Designed for LaunchAgent (StartInterval=60s) running as marco-user.
# Telegram-alert via safe_telegram_send.sh — direct path (do NOT route through
# outbox, since outbox may be the failing component being monitored).
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# === Config ===
LABEL="${WORKER_LAUNCHAGENT_LABEL:-de.marcoschmid.hermes-notification-worker}"
PLIST="${WORKER_LAUNCHAGENT_PLIST:-${HOME}/Library/LaunchAgents/${LABEL}.plist}"
HEARTBEAT_FILE="${WORKER_HEARTBEAT_FILE:-${HOME}/.openclaw/run/notification-worker.heartbeat}"
HEARTBEAT_STALE_SECONDS="${WORKER_HEARTBEAT_STALE_SECONDS:-120}"
OUTBOX_DB="${HERMES_OUTBOX_DB:-${HOME}/.hermes/outbox.db}"
STAGNATION_THRESHOLD="${WORKER_STAGNATION_THRESHOLD:-50}"
STAGNATION_AGE_SECONDS="${WORKER_STAGNATION_AGE_SECONDS:-1800}"  # 30min
STATE_DIR="${WORKER_WATCHDOG_STATE_DIR:-${HOME}/.openclaw/run/notification-worker-watchdog}"
LOG_FILE="${WORKER_WATCHDOG_LOG:-${HOME}/.openclaw/logs/notification-worker-watchdog.log}"
SAFE_SEND="${SAFE_SEND:-${HOME}/.openclaw/workspace/scripts/safe_telegram_send.sh}"
TELEGRAM_TARGET="${TELEGRAM_TARGET:-128314698}"
ALERT_COOLDOWN_SECONDS="${WORKER_WATCHDOG_ALERT_COOLDOWN:-3600}"  # 1h per-issue

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

log() {
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*" >> "$LOG_FILE"
}

# Dedupe alerts: 1 per issue per ALERT_COOLDOWN_SECONDS
alert() {
  local kind="$1" msg="$2"
  local stamp_file="${STATE_DIR}/last-alert-${kind}"
  local now_epoch last_epoch
  now_epoch=$(date +%s)
  if [ -f "$stamp_file" ]; then
    last_epoch=$(cat "$stamp_file" 2>/dev/null || echo "0")
    if [ $((now_epoch - last_epoch)) -lt "$ALERT_COOLDOWN_SECONDS" ]; then
      log "alert suppressed (cooldown): $kind"
      return 0
    fi
  fi
  echo "$now_epoch" > "$stamp_file"
  log "ALERT $kind: $msg"
  /Users/marco/.openclaw/bin/notify \
    --source notification-worker-watchdog \
    --severity error \
    --category system \
    --direct \
    --dedupe-key "worker-watchdog:${kind}:$(date +%Y-%m-%d-%H)" \
    --message "Notification-Worker-Watchdog: ${msg}" \
    >/dev/null 2>&1 || log "notify failed"
}

restart_worker() {
  local reason="$1"
  log "restart_worker: $reason"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  sleep 1
  if [ -f "$PLIST" ]; then
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>&1 | tee -a "$LOG_FILE"
  else
    log "ERROR: plist not found at $PLIST"
    alert "missing-plist" "Worker plist missing at $PLIST; cannot restart."
    return 1
  fi
}

# === Check 1: LaunchAgent loaded? ===
if ! launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  log "LaunchAgent NOT loaded: ${LABEL}"
  alert "not-loaded" "Worker LaunchAgent not loaded — attempting bootstrap."
  restart_worker "not-loaded"
  exit 0
fi

# === Check 2: Heartbeat freshness ===
if [ ! -f "$HEARTBEAT_FILE" ]; then
  log "Heartbeat file missing: $HEARTBEAT_FILE (worker just started or never wrote)"
  # Don't restart immediately — could be cold start. Just warn.
  exit 0
fi

heartbeat_mtime=$(stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || echo "0")
now_epoch=$(date +%s)
age=$((now_epoch - heartbeat_mtime))

if [ "$age" -gt "$HEARTBEAT_STALE_SECONDS" ]; then
  log "Heartbeat STALE: ${age}s > ${HEARTBEAT_STALE_SECONDS}s"
  alert "stale-heartbeat" "Worker heartbeat stale (${age}s old). Restarting."
  restart_worker "stale-heartbeat"
  exit 0
fi

# === Check 3: Outbox backlog ===
if [ -f "$OUTBOX_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  cutoff_iso=$(date -u -v-${STAGNATION_AGE_SECONDS}S '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
    || date -u -d "@$((now_epoch - STAGNATION_AGE_SECONDS))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
    || echo "")
  if [ -n "$cutoff_iso" ]; then
    stuck=$(sqlite3 "$OUTBOX_DB" \
      "SELECT COUNT(*) FROM outbox WHERE status='pending' AND created_at < '$cutoff_iso'" \
      2>/dev/null || echo "0")
    if [ "$stuck" -gt "$STAGNATION_THRESHOLD" ]; then
      log "Outbox backlog: $stuck rows older than ${STAGNATION_AGE_SECONDS}s"
      alert "backlog" "Outbox backlog: ${stuck} pending rows older than $((STAGNATION_AGE_SECONDS/60))min. Hub/MC down?"
    fi
  fi
fi

log "OK heartbeat_age=${age}s"
exit 0
