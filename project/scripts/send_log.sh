#!/usr/bin/env bash
set -euo pipefail
usage(){ echo "Usage: send_log.sh --profile <id> --log-file <path> [--summary <text>] [--channel-id <telegram_chat_id>]"; }
PROFILE=""; LOG_FILE=""; SUMMARY=""; CHANNEL_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --log-file) LOG_FILE="${2:-}"; shift 2 ;;
    --summary) SUMMARY="${2:-}"; shift 2 ;;
    --channel-id) CHANNEL_ID="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done
[[ -n "$PROFILE" && -n "$LOG_FILE" ]] || { usage >&2; exit 1; }
[[ -f "$LOG_FILE" ]] || { echo "Log file not found: $LOG_FILE" >&2; exit 1; }
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$PROJECT_DIR/profiles/$PROFILE/config.env"
[[ -f "$CFG" ]] || { echo "Config not found: $CFG" >&2; exit 1; }
# shellcheck source=/dev/null
source "$CFG"
[[ -n "$CHANNEL_ID" ]] || CHANNEL_ID="${LOG_CHANNEL_CHAT_ID:-}"
[[ -n "$CHANNEL_ID" ]] || { echo "Missing LOG_CHANNEL_CHAT_ID in $CFG" >&2; exit 1; }
[[ "$CHANNEL_ID" =~ ^-100[0-9]{6,}$ ]] || { echo "Invalid Telegram channel id: $CHANNEL_ID" >&2; exit 1; }
if [[ -n "$SUMMARY" ]]; then
  openclaw message send --channel telegram --target "$CHANNEL_ID" --message "$SUMMARY"
fi
openclaw message send --channel telegram --target "$CHANNEL_ID" --path "$LOG_FILE" --caption "$(basename "$PROJECT_DIR") log"
echo "sent_to=$CHANNEL_ID"
