# Hub Integration

## Mount on existing Hermes FastAPI app

The hub router (`gateway/hub/api.py`) provides `GET /v1/health` and `POST /v1/notifications`.
To activate it, add the following to whichever module instantiates the host `FastAPI` app
(currently `gateway/paperclip_notify_server.py`):

```python
# Phase 1 Notification Hub mount
from gateway.hub.mount import register_hub_routes
register_hub_routes(app)
```

Place after the line `app = FastAPI(...)`.

## Required env-vars

- `MC_HUB_TOKEN`: Bearer-token for Hermes-to-MC audit-push calls (must match an entry in MC `api_keys`)
- `MC_BASE_URL` (optional, default `http://127.0.0.1:3334`): MC base URL

Set in `~/.openclaw/secrets/env/notification-hub-tokens.env` and source via LaunchAgent-wrapper-script.

## LaunchAgent reload after env-var change

```bash
launchctl bootout gui/$(id -u)/de.marcoschmid.hermes-paperclip-notify 2>&1 || true
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.marcoschmid.hermes-paperclip-notify.plist
sleep 3
curl -s http://127.0.0.1:8765/v1/health | jq
```

Per memory `feedback_launchctl_plist_reload.md`: bootout/bootstrap reads env-vars fresh.
`kickstart -k` does NOT reload env-vars.

## Verification

After mount + restart:

```bash
curl -s http://127.0.0.1:8765/v1/health
# Expected: {"data":{"status":"healthy","version":"0.1.0"}}

curl -X POST http://127.0.0.1:8765/v1/notifications \
  -H "Authorization: Bearer test-source-token" \
  -H "Content-Type: application/json" \
  -d '{
    "source_slug": "weekly-preview",
    "topic": "home.kalender.weekly",
    "severity": "info",
    "audience": "family",
    "title": "Test",
    "body": "Hub test"
  }'
# Expected: 200 OK with delivered status (or 401/403/404 depending on registry state)
```
