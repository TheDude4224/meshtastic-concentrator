#!/usr/bin/env bash
# Wrapper script for OpenClaw to invoke the Meshtastic skill.
# Usage: meshtastic_cli.sh <command> [args_json]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${MESHTASTIC_CONFIG:-$SCRIPT_DIR/config.json}"

if [ ! -f "$CONFIG" ]; then
    echo '{"ok": false, "error": "Config not found. Copy config.json.template to config.json"}'
    exit 1
fi

COMMAND="${1:-help}"
ARGS="${2:-{}}"

exec python3 "$SCRIPT_DIR/meshtastic_api.py" --config "$CONFIG" --command "$COMMAND" --args "$ARGS"
