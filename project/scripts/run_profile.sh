#!/usr/bin/env bash
set -euo pipefail
WS="$HOME/.openclaw/workspace"
CFG_ROOT="$WS/projects/podcast2obsidian/profiles"
PY="${P2O_PY:-$WS/skills/podcast2obsidian/scripts/xiaoyuzhou_dl.py}"
BILIBILI_PY="${P2O_BILIBILI_PY:-$WS/skills/podcast2obsidian/scripts/bilibili_video.py}"
YOUTUBE_PY="${P2O_YOUTUBE_PY:-$WS/skills/podcast2obsidian/scripts/youtube_video.py}"
TOOLS_PY="${P2O_TOOLS_PY:-$HOME/.local/venvs/openclaw-tools/bin/python}"
usage(){
  cat <<'EOF'
Usage:
  run_profile.sh --profile <id> [--url <episode_or_podcast_url>] [--notify-chat-id <telegram_chat_id>] [--no-notify] [--dry-run]
  run_profile.sh --profile bilibili [--url <bilibili_video_url_or_bv>] [--force-audio] [--local] [--no-notebooklm]
  run_profile.sh --profile youtube [--url <youtube_video_url_or_id>] [--local] [--no-notebooklm]
  run_profile.sh --profile <id> --scan-drive-duplicates [--target <podcast_url_or_name>]
  run_profile.sh --profile <id> --dedupe-drive [--target <podcast_url_or_name>] [--apply] [--dry-run]
EOF
}
PROFILE=""; DRY=0; MODE="run"; TARGET=""; APPLY=0
NO_INFOGRAPHIC=0
FORCE_AUDIO_CLI=0
LOCAL_CLI=0
NO_NOTEBOOKLM_CLI=0
NOTIFY_CHAT_ID=""; NO_NOTIFY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --url) TARGET="$2"; shift 2;;
    --notify-chat-id) NOTIFY_CHAT_ID="$2"; shift 2;;
    --no-notify) NO_NOTIFY=1; shift;;
    --dry-run) DRY=1; shift;;
    --local) LOCAL_CLI=1; shift;;
    --no-notebooklm) NO_NOTEBOOKLM_CLI=1; shift;;
    --scan-drive-duplicates) MODE="scan-drive-duplicates"; shift;;
    --dedupe-drive) MODE="dedupe-drive"; shift;;
    --target) TARGET="$2"; shift 2;;
    --apply) APPLY=1; shift;;
    --no-infographic) NO_INFOGRAPHIC=1; shift;;
    --force-audio) FORCE_AUDIO_CLI=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown: $1"; usage; exit 2;;
  esac
 done
[[ -n "$PROFILE" ]] || { usage; exit 2; }
CFG="$CFG_ROOT/$PROFILE/config.env"; [[ -f "$CFG" ]] || { echo "Missing config: $CFG"; exit 1; }
# Export profile config so Python workers see NLM_PROFILE and related runtime flags.
set -a
# shellcheck source=/dev/null
source "$CFG"
set +a

if [[ -z "$NOTIFY_CHAT_ID" ]]; then
  NOTIFY_CHAT_ID="${RESULT_NOTIFY_CHAT_ID:-}"
fi

if [[ -z "$TARGET" ]]; then
  TARGET="${URL:-}"
fi

if [[ "$MODE" == "run" ]]; then
  [[ -n "$TARGET" ]] || { echo "Missing target URL. Pass --url or set URL in $CFG"; exit 1; }
  [[ "$TARGET" != *"REPLACE_ME"* ]] || {
    echo "Config placeholder still present in $CFG (URL contains REPLACE_ME). Pass --url explicitly or update config first."
    exit 1
  }
fi

case "$MODE" in
  run)
    if [[ "$PROFILE" == "bilibili" ]]; then
      CMD=(python3 -u "$BILIBILI_PY" "$TARGET")
      [[ "${LOCAL_ONLY:-0}" == "1" || "$LOCAL_CLI" == "1" ]] && CMD+=(--local)
      [[ "${NO_NOTEBOOKLM:-0}" == "1" || "$NO_NOTEBOOKLM_CLI" == "1" ]] && CMD+=(--no-notebooklm)
      [[ "${FORCE_AUDIO:-0}" == "1" || "$FORCE_AUDIO_CLI" == "1" ]] && CMD+=(--force-audio)
      [[ "$NO_INFOGRAPHIC" == "1" ]] && CMD+=(--no-infographic)
    elif [[ "$PROFILE" == "youtube" ]]; then
      CMD=("$TOOLS_PY" -u "$YOUTUBE_PY" "$TARGET")
      [[ "${LOCAL_ONLY:-0}" == "1" || "$LOCAL_CLI" == "1" ]] && CMD+=(--local)
      [[ "${NO_NOTEBOOKLM:-0}" == "1" || "$NO_NOTEBOOKLM_CLI" == "1" ]] && CMD+=(--no-notebooklm)
      [[ "$NO_INFOGRAPHIC" == "1" ]] && CMD+=(--no-infographic)
    else
      CMD=(python3 -u "$PY" "$TARGET")
      [[ "${LOCAL_ONLY:-0}" == "1" || "$LOCAL_CLI" == "1" ]] && CMD+=(--local)
      [[ "${NO_NOTEBOOKLM:-0}" == "1" || "$NO_NOTEBOOKLM_CLI" == "1" ]] && CMD+=(--no-notebooklm)
      [[ "${FORCE_ALL:-0}" == "1" ]] && CMD+=(--yes)
      [[ "$NO_INFOGRAPHIC" == "1" ]] && CMD+=(--no-infographic)
    fi
    ;;
  scan-drive-duplicates)
    CMD=(python3 -u "$PY" --scan-drive-duplicates "$TARGET")
    ;;
  dedupe-drive)
    CMD=(python3 -u "$PY" --dedupe-drive "$TARGET")
    [[ "$APPLY" == "1" ]] && CMD+=(--apply)
    ;;
  *)
    echo "Unknown mode: $MODE"; exit 2;;
esac

if [[ "$DRY" == "1" ]]; then
  printf 'Command:\n  '
  printf '%q ' "${CMD[@]}"
  printf '\n'
  if [[ "$NO_NOTIFY" != "1" && -n "$NOTIFY_CHAT_ID" ]]; then
    printf 'Notify:\n  telegram %q\n' "$NOTIFY_CHAT_ID"
  fi
  exit 0
fi

LOG_DIR="$WS/projects/podcast2obsidian/logs"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
LOG_FILE="$LOG_DIR/${PROFILE}-${MODE}-${RUN_ID}.log"

notify_result() {
  local exit_code="$1"
  [[ "$NO_NOTIFY" == "1" ]] && return 0
  [[ -n "$NOTIFY_CHAT_ID" ]] || return 0
  [[ "$MODE" == "run" ]] || return 0
  if ! [[ "$NOTIFY_CHAT_ID" =~ ^-?[0-9]{6,}$ ]]; then
    echo "notify skipped: invalid RESULT_NOTIFY_CHAT_ID=$NOTIFY_CHAT_ID" >&2
    return 0
  fi

  local result_line podcast title notebook_id note_id note_path infographic_ok summary
  result_line="$(grep '📣 RESULT' "$LOG_FILE" | tail -1 || true)"
  podcast="$(grep -E '^[[:space:]]*📻 节目:' "$LOG_FILE" | tail -1 | sed -E 's/^[[:space:]]*📻 节目:[[:space:]]*//' || true)"
  title="$(grep -E '^[[:space:]]*📝 标题:' "$LOG_FILE" | tail -1 | sed -E 's/^[[:space:]]*📝 标题:[[:space:]]*//' || true)"

  if [[ -n "$result_line" ]]; then
    notebook_id="$(sed -nE 's/.*notebook_id=([^ ]+).*/\1/p' <<<"$result_line")"
    note_id="$(sed -nE 's/.*note_id=([^ ]+).*/\1/p' <<<"$result_line")"
    note_path="$(sed -nE 's/.*note_path=(.*)$/\1/p' <<<"$result_line" | sed -E 's/[[:space:]]+(query_ok|fast_note_ok|infographic_ok|reason|bvid|cid|video_id|subtitle_lang|subtitle_language|subtitle_source|mode|audio_path)=.*$//')"
    infographic_ok="$(sed -nE 's/.*infographic_ok=([^ ]+).*/\1/p' <<<"$result_line")"
  fi

  local task_label="Podcast"
  [[ "$PROFILE" == "bilibili" ]] && task_label="Bilibili"
  [[ "$PROFILE" == "youtube" ]] && task_label="YouTube"

  if [[ "$exit_code" == "0" && "$result_line" == *"status=success"* ]]; then
    summary="完成：${podcast:-$task_label} - ${title:-$TARGET}"
    [[ -n "$notebook_id" && "$notebook_id" != "none" ]] && summary+=$'\n'"• NotebookLM: $notebook_id"
    [[ -n "$note_id" && "$note_id" != "none" ]] && summary+=$'\n'"• Note ID: $note_id"
    [[ -n "$note_path" && "$note_path" != "none" ]] && summary+=$'\n'"• 路径: $note_path"
    [[ -n "$infographic_ok" ]] && summary+=$'\n'"• 信息图: $([[ "$infographic_ok" == "1" ]] && echo 已触发 || echo 未确认)"
  elif [[ -n "$result_line" ]]; then
    summary="$task_label 任务结束但未成功：${podcast:-$task_label} - ${title:-$TARGET}"$'\n'"• exit_code: $exit_code"$'\n'"• RESULT: $result_line"$'\n'"• log: $LOG_FILE"
  else
    summary="$task_label 任务失败/未返回 RESULT：${TARGET}"$'\n'"• exit_code: $exit_code"$'\n'"• log: $LOG_FILE"$'\n'"• tail:"$'\n'"$(tail -20 "$LOG_FILE" | sed 's/\r//g')"
  fi

  openclaw message send --channel telegram --target "$NOTIFY_CHAT_ID" --message "$summary" \
    || echo "notify failed for $NOTIFY_CHAT_ID" >&2
}

set +e
"${CMD[@]}" 2>&1 | tee "$LOG_FILE"
STATUS=${PIPESTATUS[0]}
set -e
notify_result "$STATUS"
exit "$STATUS"
