#!/bin/bash
# Single-shot LaunchAgent runner für gateway.db_cleanup (P4 followup).
#
# Daily 04:00 via StartCalendarInterval. Deletes terminal-state rows older
# than retention windows (sent>30d, processed>30d, failed>90d, ignored>7d).
# Dead-lettered rows ARE NEVER auto-deleted.
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

exec "${VENV_PYTHON}" -m gateway.db_cleanup_entrypoint
