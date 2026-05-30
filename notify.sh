#!/usr/bin/env bash
# notify.sh "message" — post a message to the configured Slack webhook (with mention prefix).
# Reads the webhook + mention from ~/.claude/.omc-config.json (set via OMC configure-notifications).
# Usage:  ./notify.sh "Stage A done: baseline seq-read = 3.1 GB/s, no errors."
set -euo pipefail

CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.omc-config.json"
MSG="${*:-(no message)}"

URL="$(jq -r '.notifications.slack.webhookUrl // empty' "$CONFIG" 2>/dev/null || true)"
MENTION="$(jq -r '.notifications.slack.mention // empty' "$CONFIG" 2>/dev/null || true)"

if [ -z "$URL" ]; then
    echo "notify.sh: no Slack webhook configured in $CONFIG" >&2
    exit 1
fi

TEXT="$MSG"
[ -n "$MENTION" ] && TEXT="${MENTION}"$'\n'"${MSG}"

PAYLOAD="$(jq -nc --arg t "$TEXT" '{text: $t}')"
curl -s -o /dev/null -w "slack: HTTP %{http_code}\n" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" "$URL"
