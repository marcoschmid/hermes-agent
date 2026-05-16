# Hub Sender Spec

This document defines the producer-side contract for posting notifications to
the Hermes Notification Hub.

## Endpoint

Senders POST JSON to:

```text
POST {hub_url}/v1/notifications
```

`hub_url` is the deployed hub base URL, for example
`http://127.0.0.1:8000`. The Python helper also accepts the full
`.../v1/notifications` endpoint.

## HMAC Contract

The authentication algorithm is HMAC-SHA256.

Sign the exact request body bytes that will be sent over HTTP. For JSON, do not
sign a pretty-printed body and then send a reformatted body.

Canonical message:

```text
{timestamp}
{nonce}
{body_sha256_hex}
```

Where:

- `timestamp` is the exact `X-Hub-Timestamp` header value.
- `nonce` is the exact `X-Hub-Nonce` header value.
- `body_sha256_hex` is the lowercase hex SHA-256 digest of the request body bytes.

Compute:

```text
X-Hub-Signature = hex(HMAC_SHA256(hub_secret_bytes, canonical_message_utf8))
```

The signature header value must be lowercase hex.

## Required Headers

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-Hub-Timestamp` | ISO-8601 UTC timestamp with `Z`, for example `2026-05-16T15:30:00Z` |
| `X-Hub-Nonce` | UUIDv4 string or random 128-bit hex string |
| `X-Hub-Signature` | Lowercase hex HMAC-SHA256 signature |

The server accepts timestamp drift of 60 seconds. Generate the timestamp when
the request is sent and keep producer hosts time-synchronized.

The nonce must be unique per source within the server replay window, currently
about 1 hour. Retries should generate a new timestamp, nonce, and signature.
Use `dedupe_key` to make a retried event idempotent at the notification layer.

## JSON Body

Required fields:

```json
{
  "source_slug": "paperclip",
  "topic": "ops.deploy",
  "severity": "info",
  "audience": "marco",
  "title": "Deploy complete",
  "body": "Production deploy finished."
}
```

Optional fields:

```json
{
  "urgency": "none",
  "actionability": "info",
  "dedupe_key": "deploy-2026-05-16T1530Z",
  "correlation_id": "ci-run-123",
  "payload": {
    "service": "api",
    "version": "2026.05.16"
  }
}
```

Current enum values:

| Field | Values |
| --- | --- |
| `severity` | `debug`, `info`, `notice`, `warn`, `error`, `crit` |
| `urgency` | `none`, `today`, `soon`, `now` |
| `actionability` | `info`, `ack`, `decide`, `task` |

## Responses

Success responses are `200` or `201` and contain a JSON object with a `data`
field:

```json
{
  "data": {
    "event_id": "evt_123",
    "status": "queued"
  }
}
```

Errors are JSON with a `detail` object:

```json
{
  "detail": {
    "error_code": "bad_signature",
    "message": "HMAC signature mismatch"
  }
}
```

## Error Codes

| Error code | HTTP status | Meaning |
| --- | ---: | --- |
| `missing_auth` | 401 | No accepted auth path was supplied. |
| `missing_header` | 422 | One or more required HMAC header values are empty. |
| `bad_timestamp` | 422 | `X-Hub-Timestamp` could not be parsed. |
| `stale_timestamp` | 401 | Timestamp drift exceeds 60 seconds. |
| `bad_signature` | 401 | Signature does not match the timestamp, nonce, body, and source secret. |
| `replay_detected` | 401 | Nonce was already used for the source in the replay window. |
| `unknown_source` | 401 | `source_slug` is not registered. |
| `no_hub_secret` | 401 | Registered source has no hub secret configured. |
| `source_disabled` | 403 | Source is registered but disabled. |
| `topic_not_found` | 404 | Topic slug is not registered. |
| `scope_violation` | 403 | Source is not allowed to send the requested topic or severity. |

Clients should treat any non-`200`/`201` response as failed and surface both the
status code and `detail`.

## Python Example

```python
import asyncio
import os

from gateway.hub.sender_client import HubSenderClient


async def main() -> None:
    client = HubSenderClient(
        hub_url=os.environ["HUB_URL"],
        source_slug=os.environ["HUB_SOURCE_SLUG"],
        hub_secret=os.environ["HUB_SECRET"].encode("utf-8"),
    )
    try:
        result = await client.send(
            topic="ops.deploy",
            audience="marco",
            title="Deploy complete",
            body="Production deploy finished.",
            severity="info",
            urgency="none",
            actionability="info",
            dedupe_key="deploy-2026-05-16T1530Z",
            correlation_id="ci-run-123",
            payload={"service": "api"},
        )
        print(result)
    finally:
        await client.close()


asyncio.run(main())
```

## Bash Example

This example signs the exact compact JSON string stored in `BODY`.

```bash
#!/usr/bin/env bash
set -euo pipefail

: "${HUB_URL:?set HUB_URL, for example http://127.0.0.1:8000}"
: "${HUB_SOURCE_SLUG:?set HUB_SOURCE_SLUG}"
: "${HUB_SECRET:?set HUB_SECRET}"

BODY="$(jq -cn \
  --arg source_slug "$HUB_SOURCE_SLUG" \
  --arg topic "ops.deploy" \
  --arg severity "info" \
  --arg audience "marco" \
  --arg title "Deploy complete" \
  --arg body "Production deploy finished." \
  '{
    source_slug: $source_slug,
    topic: $topic,
    severity: $severity,
    audience: $audience,
    title: $title,
    body: $body
  }')"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
NONCE="$(python -c 'import uuid; print(uuid.uuid4())')"
BODY_SHA256="$(printf '%s' "$BODY" | shasum -a 256 | awk '{print $1}')"
CANONICAL="$(printf '%s\n%s\n%s' "$TIMESTAMP" "$NONCE" "$BODY_SHA256")"
SIGNATURE="$(printf '%s' "$CANONICAL" \
  | openssl dgst -sha256 -hmac "$HUB_SECRET" -hex \
  | awk '{print $2}')"

curl -sS -X POST "${HUB_URL%/}/v1/notifications" \
  -H "Content-Type: application/json" \
  -H "X-Hub-Timestamp: $TIMESTAMP" \
  -H "X-Hub-Nonce: $NONCE" \
  -H "X-Hub-Signature: $SIGNATURE" \
  --data-binary "$BODY"
```
