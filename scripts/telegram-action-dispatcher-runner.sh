#!/bin/bash
# LaunchAgent runner für gateway.telegram_action_dispatcher (P4 Track B).
#
# Loaded by ~/Library/LaunchAgents/de.marcoschmid.hermes-telegram-action-dispatcher.plist.
# Sources notification-hub-tokens.env (TELEGRAM_CALLBACKS_DB optional override)
# and runs dispatcher as marco-user. Dispatcher polls telegram_callbacks +
# invokes per-callback_type handlers (approve/reject/skip) with retry+dead-letter.
set -euo pipefail

ENV_FILE="${HOME}/.openclaw/secrets/env/notification-hub-tokens.env"
HERMES_AGENT_ROOT="${HOME}/Code/hermes-agent"

if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

VENV_PYTHON="${HERMES_AGENT_ROOT}/.venv/bin/python"
if [ ! -x "${VENV_PYTHON}" ]; then
  echo "ERROR: hermes-agent venv missing at ${VENV_PYTHON}" >&2
  exit 1
fi

export PYTHONPATH="${HERMES_AGENT_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

: "${TELEGRAM_CALLBACKS_DB:=${HOME}/.hermes/telegram_callbacks.db}"
: "${TELEGRAM_DISPATCHER_POLL_INTERVAL:=5}"
: "${TELEGRAM_DISPATCHER_BATCH_SIZE:=25}"

export TELEGRAM_CALLBACKS_DB
export TELEGRAM_DISPATCHER_POLL_INTERVAL
export TELEGRAM_DISPATCHER_BATCH_SIZE

exec "${VENV_PYTHON}" -m gateway.telegram_action_dispatcher_entrypoint
