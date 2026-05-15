#!/usr/bin/env bash
set -euo pipefail

LABEL="de.openclaw.hermes-skills-watch"
DOMAIN="gui/$(id -u)"
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SOURCE="$REPO_ROOT/packaging/launchagents/${LABEL}.plist"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

echo "Installing ${LABEL}"
echo "Repo root: ${REPO_ROOT}"
echo "Source: ${SOURCE}"
echo "Destination: ${DEST}"

command -v xmllint >/dev/null
xmllint --noout "$SOURCE"
echo "✓ plist XML is valid"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/.openclaw/logs"
mkdir -p "$HOME/.hermes/skills"
echo "✓ runtime directories exist"

echo "Booting out existing LaunchAgent if loaded"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true

cp "$SOURCE" "$DEST"
echo "✓ plist copied"

echo "Bootstrapping LaunchAgent"
launchctl bootstrap "$DOMAIN" "$DEST"
echo "✓ LaunchAgent bootstrapped"

echo "LaunchAgent status:"
launchctl print "${DOMAIN}/${LABEL}" | head -30

echo "✓ install complete"
echo "Log: $HOME/.openclaw/logs/hermes-skills-watch.log"
echo "Verify: launchctl print ${DOMAIN}/${LABEL}"
