#!/bin/bash
# LaunchAgent runner for gateway.notification_worker (P4 Track C).
#
# Loaded by ~/Library/LaunchAgents/de.marcoschmid.hermes-notification-worker.plist.
# Sources notification-hub-tokens.env (HUB / MC tokens) and runs the worker
# as marco user. Worker pollt OutboxStore + dispatched via per-channel
# FallbackNotificationRouter.
set -euo pipefail

ENV_FILE="${HOME}/.openclaw/secrets/env/notification-hub-tokens.env"
PUSHOVER_ENV_FILE="${HOME}/.openclaw/secrets/env/pushover.env"
HERMES_AGENT_ROOT="${HOME}/Code/hermes-agent"

if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# Pushover credentials (PUSHOVER_API_TOKEN / PUSHOVER_USER_KEY) for the
# standalone pushover channel router. Kept in a separate secrets file.
if [ -f "${PUSHOVER_ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${PUSHOVER_ENV_FILE}"
  set +a
fi

# Hermes-agent venv for Python deps (requests, sqlite3 incl).
VENV_PYTHON="${HERMES_AGENT_ROOT}/.venv/bin/python"
if [ ! -x "${VENV_PYTHON}" ]; then
  echo "ERROR: hermes-agent venv missing at ${VENV_PYTHON}" >&2
  exit 1
fi

export PYTHONPATH="${HERMES_AGENT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# Default config — override via plist EnvironmentVariables if needed.
: "${HERMES_OUTBOX_DB:=${HOME}/.hermes/outbox.db}"
: "${HERMES_NOTIFICATION_WORKER_POLL_INTERVAL:=10}"
: "${HERMES_NOTIFICATION_WORKER_BATCH_SIZE:=25}"
: "${HERMES_NOTIFICATION_WORKER_ZOMBIE_TIMEOUT:=300}"

export HERMES_OUTBOX_DB
export HERMES_NOTIFICATION_WORKER_POLL_INTERVAL
export HERMES_NOTIFICATION_WORKER_BATCH_SIZE
export HERMES_NOTIFICATION_WORKER_ZOMBIE_TIMEOUT

exec "${VENV_PYTHON}" -m gateway.notification_worker_entrypoint
