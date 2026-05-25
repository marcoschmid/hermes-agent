# gateway/ — Hermes Notification-Gateway (Phase-4 Architektur)

Persistent + cascading + auditable Notification-Delivery für Hermes-Agent.

14 PRs merged 2026-05-23 bis 2026-05-25 (Phase-4 + Round-3 hardening). 135+ contract-tests grün.

---

## Module-Overview

| Modul | Verantwortung | Tests |
|---|---|---|
| `outbox.py` | SQLite-backed queue: dedup + claim_token-fence + retry + dead-letter | 14 |
| `telegram_gateway.py` | Telegram-Bot-API webhook-callback receiver (approve/reject/skip) | 5 |
| `fallback_channels.py` | 3-Stage cascade Router: hermes → mission-control → direct | 5 |
| `router_factory.py` | Sender-builders für Hub/MC/safe_telegram + Hub-HMAC support | 25 |
| `notification_worker.py` | OutboxStore consumer-loop, channel-specific router-dispatch | 17 |
| `telegram_action_dispatcher.py` | telegram_callbacks consumer-loop, action-handler-dispatch | 8 |
| `db_cleanup.py` | Retention-driven cleanup für outbox + telegram_callbacks | 19 |
| `outbox_cli.py` | `outbox-enqueue` CLI für bash-callers (drop-in für safe_telegram_send) | 17 |
| `hub/telegram_webhook_route.py` | FastAPI POST /telegram/webhook mount | 9 |

---

## Architektur-Diagramm

```
                       PRODUCERS
       ┌───────────────────┬───────────────────────────────┐
       │                   │                                │
  bash-callers       Python-callers              direct safe_telegram_send
       │                   │                  (15 legacy callers — Track A2)
       │ outbox-enqueue    │ router.send()
       │ --channel X       │ or outbox.enqueue()
       ▼                   ▼
  ┌─────────────────────────────────────────┐
  │  OutboxStore (~/.hermes/outbox.db)      │
  │  - dedup_key UNIQUE                     │
  │  - claim_token UUID fence               │
  │  - status: pending/claimed/sent/        │
  │    failed/dead-lettered                 │
  └─────────────────────────────────────────┘
                   │
                   ▼ (poll every 10s)
  ┌─────────────────────────────────────────┐
  │  NotificationWorker                      │
  │  - LaunchAgent de.marcoschmid.hermes-   │
  │    notification-worker                   │
  │  - claim_due (atomic)                   │
  │  - dispatch via channel-router-factory  │
  │  - mark_sent / record_failure           │
  │  - heartbeat → watchdog                  │
  └─────────────┬───────────────────────────┘
                │
                ▼ per-channel router_factory
  ┌─────────────────────────────────────────┐
  │  FallbackNotificationRouter 3-stage      │
  │  ┌─ hermes ──────────────────────────┐  │
  │  │  Hub :8766/v1/notifications        │  │
  │  │  Bearer HUB_PILOT_TOKEN OR         │  │
  │  │  HMAC-SHA256 per-source (v4b)      │  │
  │  └────────────────────────────────────┘  │
  │  │ fail                                  │
  │  ▼                                       │
  │  ┌─ mission-control ─────────────────┐  │
  │  │  MC :3334/api/board/.../events    │  │
  │  │  Bearer MC_HUB_TOKEN              │  │
  │  │  (audit-only, kein User-Push)     │  │
  │  └────────────────────────────────────┘  │
  │  │ fail                                  │
  │  ▼                                       │
  │  ┌─ direct-fallback ─────────────────┐  │
  │  │  safe_telegram_send.sh subprocess │  │
  │  │  openclaw-CLI + api.telegram.org  │  │
  │  └────────────────────────────────────┘  │
  └─────────────────────────────────────────┘
                   │
                   ▼ run-log JSONL
       ~/.openclaw/run/fallback-notification-router.jsonl
       (per-hop audit-trail)


  REVERSE FLOW (Telegram → action):

  Telegram Bot @mymoltjarvisbot
       │ button-click
       ▼
  Hub :8766 POST /telegram/webhook
       │ asyncio.to_thread (Round-2 HIGH-3)
       ▼
  TelegramCallbackReceiver.handle_webhook
       - X-Telegram-Bot-Api-Secret-Token verify
       - rate-limit per user
       - persist row → telegram_callbacks (~/.hermes/telegram_callbacks.db)
       │
       ▼ (Telegram <500ms response gate)
  Return 200 to Telegram

  ──────── async dispatch (decoupled) ────────

  TelegramActionDispatcher (LaunchAgent, 5s poll)
       - claim_due (UPDATE...RETURNING claim_token fence)
       - handler-dispatch: approve/reject/skip
       - mark_processed | record_transient_failure (retry+backoff)
       - dead-letter at 5 attempts
       │
       ▼
  paperclip_issue_action stub (Marco wires when client-lib ready)
```

---

## Producer-Patterns (3)

### 1. Python in-process — direct Router

```python
from gateway.router_factory import make_default_router

router = make_default_router(
    source_slug="weekly-preview",
    target_chat_id="128314698",
    context="weekly-preview",
    hub_auth_mode="hmac",
    hub_hmac_secret_env="WEEKLY_PREVIEW_HUB_HMAC_SECRET",
)
result = router.send(
    message="Wochenvorschau …",
    issue={"id": "weekly-2026-W21", "title": "Wochenvorschau", "audience": "family"},
)
if not result.ok:
    log.warning("Cascade failed at %s: %s", result.hop, result.error)
```

**Use when**: short-lived script, latency-OK with cascade-wait (up to ~30s worst-case).

### 2. Bash callers — outbox-enqueue CLI

```bash
"$OUTBOX_CLI" \
  --channel telegram \
  --target "$TELEGRAM_CHAT_ID" \
  --context cert_expiry \
  --dedupe-key "cert-expiry:$(date +%Y-%m-%d)" \
  --message "$msg"
# returns exit=0 with {"ok": true, "row_id": "...", "deduped": false}
# Worker dispatches async; caller returns immediately.
```

Legacy `safe_telegram_send.sh` args fully supported: `--dedupe-window`, `--rate-limit-window`, `--buttons`, `--media` accepted-and-ignored.

**Use when**: bash-cron-jobs, ops-scripts. Async-delivery semantic acceptable.

### 3. Direct Hub POST — Python production-callers (drobo-backup-style)

```python
# Native HMAC-signed POST
import json, uuid, hmac, hashlib, requests
from datetime import datetime, timezone

ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
nonce = uuid.uuid4().hex
body = json.dumps({"source_slug": "drobo-backup", "topic": "ops.backup", ...}).encode()
sig = hmac.new(secret_bytes, f"{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode(),
               hashlib.sha256).hexdigest()
requests.post("http://127.0.0.1:8766/v1/notifications",
              data=body,
              headers={"X-Hub-Timestamp": ts, "X-Hub-Nonce": nonce, "X-Hub-Signature": sig})
```

**Use when**: existing Hub-native integration (drobo-backup pattern).

---

## Consumer-Patterns (2)

### NotificationWorker — outbox-row dispatcher

LaunchAgent `de.marcoschmid.hermes-notification-worker` runs as marco-user.

**Cycle (every 10s)**:
1. `recover_zombies(timeout=300s)` — claimed-stuck rows → pending+backoff+attempts++ (dead-letter at 5)
2. `claim_due(limit=25)` — atomic UPDATE...RETURNING with fresh UUID claim_token
3. Per row: router_factory(channel) → router.send → mark_sent | record_failure
4. SIGTERM mid-batch: abandon remaining, zombie-recovery picks up
5. Heartbeat → `~/.openclaw/run/notification-worker.heartbeat`

### TelegramActionDispatcher — callback-action dispatcher

LaunchAgent `de.marcoschmid.hermes-telegram-action-dispatcher` runs as marco-user.

**Cycle (every 5s)**:
1. `claim_due` — atomic claim of accepted+pending rows with fresh claim_token
2. Per row: handler[callback_type](issue_id, row)
3. Success → mark_processed (dispatch_status='processed')
4. Transient-fail (handler raise) → attempts++ + backoff + dead-letter at 5
5. Permanent-fail (no handler) → dispatch_status='failed' (no retry)

---

## Permission-Boundary (CRITICAL pre-install)

**ALL DB-actors MUST run as SAME user** (default: `marco`). Mixing UIDs
(e.g. Hermes-Hub als `_hermes` writes to outbox.db; Worker als `marco`
reads same path) causes:
- WAL/SHM file-permission errors
- SQLite-lock-state mismatch
- Silent data-loss on writes that worker can't read back

If Hermes-Hub migrates to `_hermes` UID (per G2-Stufe-2 plan), ALSO migrate
worker + dispatcher + cleanup to `_hermes` (LaunchAgent UserName override
or LaunchDaemon under `/Library/LaunchDaemons/`). Update env-vars
`HERMES_OUTBOX_DB` + `TELEGRAM_CALLBACKS_DB` to shared path with explicit
group + ACL for both processes.

Plist templates use `/Users/marco/...` absolute paths — Marco-specific
deployment. For other users: edit plist `ProgramArguments` + adjust
`EnvironmentVariables.HOME`.

## LaunchAgent Install-Order (dependency-sensitive)

1. **Worker first** (`de.marcoschmid.hermes-notification-worker`) — creates
   outbox.db schema on first claim_due. Required-before bash-callers migrate
   to outbox-enqueue (else silent backlog).
2. **Worker-Watchdog** (`...-watchdog`) — monitors worker heartbeat.
   Requires worker-plist-path correctness in watchdog script env-vars.
3. **Telegram-Action-Dispatcher** — independent of worker; can install in
   parallel. Required before bot-webhook setWebhook cutover.
4. **DB-Cleanup** — last; both DBs must exist. Otherwise silently no-ops.

## LaunchAgents (Marco-Install)

```bash
# notification_worker (poll outbox)
cp scripts/launchagents/de.marcoschmid.hermes-notification-worker.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.marcoschmid.hermes-notification-worker.plist

# notification_worker_watchdog (60s checks + restart)
cp scripts/launchagents/de.marcoschmid.hermes-notification-worker-watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.marcoschmid.hermes-notification-worker-watchdog.plist

# telegram_action_dispatcher (poll telegram_callbacks)
cp scripts/launchagents/de.marcoschmid.hermes-telegram-action-dispatcher.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.marcoschmid.hermes-telegram-action-dispatcher.plist

# db_cleanup (daily 04:00 retention)
cp scripts/launchagents/de.marcoschmid.hermes-db-cleanup.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.marcoschmid.hermes-db-cleanup.plist

# outbox-enqueue CLI symlink (für bash-callers)
ln -sf ~/Code/hermes-agent/scripts/outbox-enqueue ~/.openclaw/bin/outbox-enqueue
```

Verify:
```bash
launchctl print gui/$(id -u)/de.marcoschmid.hermes-notification-worker | head -20
tail -f ~/.openclaw/logs/notification-worker.log
```

---

## Env-Vars

Loaded from `~/.openclaw/secrets/env/notification-hub-tokens.env` by runner-shims.

| Variable | Purpose | Default |
|---|---|---|
| `HUB_PILOT_TOKEN` | Hub Bearer (legacy, transitional) | required für Bearer-Mode |
| `HERMES_HUB_BEARER_TOKEN` | Per-caller Bearer (alias) | optional |
| `<CHANNEL>_HUB_HMAC_SECRET` | Per-source HMAC v4b | required für HMAC-Mode |
| `MC_HUB_TOKEN` | MC events-endpoint Bearer | required für MC-Stage |
| `TELEGRAM_HERMES_BOT_TOKEN` | Bot-API token | required für direct-Stage |
| `TELEGRAM_WEBHOOK_SECRET` | Webhook X-Telegram-Bot-Api-Secret-Token | required für Track B |
| `HERMES_TELEGRAM_WEBHOOK_REQUIRED` | Production-fail-startup-flag | unset = dev-skip |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated whitelist | `128314698` |
| `TELEGRAM_RATE_LIMIT_PER_MINUTE` | Per-user rate-limit | `60` |
| `TELEGRAM_FAMILY_CHAT_ID` | Direct-stage default chat-id | `128314698` |
| `HERMES_OUTBOX_DB` | Outbox SQLite path | `~/.hermes/outbox.db` |
| `TELEGRAM_CALLBACKS_DB` | Telegram callbacks SQLite path | `~/.hermes/telegram_callbacks.db` |
| `HERMES_NOTIFICATION_WORKER_*` | Worker tuning (POLL_INTERVAL, BATCH_SIZE, ZOMBIE_TIMEOUT, HEARTBEAT) | per-module-defaults |
| `TELEGRAM_DISPATCHER_*` | Dispatcher tuning | per-module-defaults |
| `HERMES_DB_CLEANUP_DRY_RUN` | Cleanup dry-run flag | unset = real-delete |

---

## DB-Schemas

### `outbox` (~/.hermes/outbox.db)

```sql
CREATE TABLE outbox (
    id            TEXT PRIMARY KEY,
    channel       TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    status        TEXT NOT NULL,    -- pending|claimed|sent|dead-lettered
    next_retry_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    claim_token   TEXT              -- UUID fence; NULL when not claimed
);
CREATE UNIQUE INDEX outbox_dedup_key_idx ON outbox(dedup_key);
CREATE INDEX outbox_due_idx ON outbox(status, next_retry_at);
CREATE INDEX outbox_cleanup_idx ON outbox(status, updated_at);
```

### `telegram_callbacks` (~/.hermes/telegram_callbacks.db)

```sql
CREATE TABLE telegram_callbacks (
    callback_id     TEXT PRIMARY KEY,
    update_id       INTEGER NOT NULL,
    user_id         TEXT NOT NULL,
    chat_id         TEXT,
    message_id      INTEGER,
    callback_type   TEXT NOT NULL,   -- approve|reject|skip|unknown
    callback_data   TEXT NOT NULL,
    issue_id        TEXT,
    received_at     TEXT NOT NULL,
    processed_at    TEXT,            -- set when dispatcher completes
    accepted        INTEGER NOT NULL,
    error           TEXT,
    -- Track B Round-2 schema
    claim_token     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    dispatch_status TEXT NOT NULL DEFAULT 'pending',
                    -- pending|claimed|processed|failed|dead-lettered|ignored
    next_retry_at   TEXT
);
CREATE INDEX telegram_callbacks_user_received_idx ON telegram_callbacks(user_id, received_at);
CREATE INDEX telegram_callbacks_cleanup_idx ON telegram_callbacks(dispatch_status, processed_at);
```

Schema-migrations are idempotent (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` with duplicate-column tolerance).

---

## Troubleshooting

### Worker not dispatching
```bash
launchctl print gui/$(id -u)/de.marcoschmid.hermes-notification-worker
# Status line "state = running"? If not, check ExitCode + StandardErrorPath.

sqlite3 ~/.hermes/outbox.db \
  "SELECT status, COUNT(*) FROM outbox GROUP BY status"
# Pending rows accumulating = worker dead OR Hub+MC+direct all failing.

cat ~/.openclaw/run/notification-worker.heartbeat
# Timestamp old? Watchdog should auto-restart within 60s.
```

### Hub returns 401 on POST /v1/notifications
- Bearer: token must equal `HUB_PILOT_TOKEN` exactly (single global). Per-source bearer-tokens NOT validated.
- HMAC: `notification_sources.hub_secret` in MC registry must match `<SOURCE>_HUB_HMAC_SECRET` env-var.

### Hub returns 200 with `status: suppressed_flapping`
By-design: Hub-Pipeline flapping-suppression. Router's `_parse_intent_response` treats missing `data.event_id` as failure → cascade to MC. Acceptable graceful degradation.

### Dead-letter rows piling up
```bash
sqlite3 ~/.hermes/outbox.db \
  "SELECT id, channel, dedup_key, last_error, created_at FROM outbox WHERE status='dead-lettered'"
```
Investigate root cause. Manual drain after review:
```bash
sqlite3 ~/.hermes/outbox.db \
  "DELETE FROM outbox WHERE status='dead-lettered' AND id IN ('<id1>', '<id2>', ...)"
```

Same for telegram_callbacks (column `dispatch_status`).

### Telegram webhook secret rotation
1. `openssl rand -hex 32` → new secret
2. Update `TELEGRAM_WEBHOOK_SECRET` in env-file
3. `launchctl kickstart -k gui/$(id -u)/com.marco.notification-hub`
4. `curl ".../setWebhook?url=...&secret_token=<new>"` to Telegram

---

## Track-Status (2026-05-25)

| Track | PR | Status |
|---|---|---|
| Module 1/3 OutboxStore | #6 | code-live |
| Module 2/3 TelegramCallbackReceiver | #7 | code-live (Track B mount applied) |
| Module 3/3 FallbackNotificationRouter | #8 | code-live |
| Track A weekly_preview Pilot | #9 + workspace | **LIVE seit 2026-05-24** (HMAC v4b) |
| Track A1.5 HMAC mode | #10 | code-live |
| Track C notification_worker | #11 | code-live; install pending |
| Track A2 Phase 1 outbox-enqueue CLI | #12 | code-live; symlink pending |
| Track C Watchdog + heartbeat | #13 | code-live; install pending |
| Track B Hub-mount + ActionDispatcher | #14 | code-live; cf-Tunnel-Decision pending |
| db_cleanup retention-jobs | #15 | code-live; install pending |

**Production-Wiring Status**: weekly_preview LIVE; alle anderen Tracks Marco-Install pending.

---

## Codex-Round-2 Total

~24 production-holes gefixt über alle PRs (2 CRITICAL + 12 HIGH + 8 MEDIUM + 2 LOW).

Pattern bestätigt (per memory `feedback_codex_round_2_mandatory`): GREEN-tests verfehlen consistent concurrency/fence/cross-module/semantic-edge-cases die Round-2 findet. Round-2-Reviews mandatory bei production-pfaden.

---

## Related Docs

- Phase-4 Plans: `workspace/projects/jarvis-os-redesign/plans/2026-05-23-p4-*.md` + `2026-05-24-p4-*.md` + `2026-05-25-p4-*.md`
- Memory: `~/.claude/projects/-Users-marco--openclaw-workspace/memory/project_p4_complete_2026_05_25.md`
- Operational LOG: `workspace/projects/jarvis-os-redesign/LOG.md`
