#!/usr/bin/env bash
set -euo pipefail

CHAT_TARGET="telegram:-5287663927"
OPENCLAW_BIN="/Users/MacBookAir/.nvm/versions/node/v22.22.0/bin/openclaw"

# Short nudge; use Jaz for structure, Yoko for tone/guardrails.
JAZ=$($OPENCLAW_BIN agent --agent jaz --local --timeout 90 --message "You are Jaz (chief of staff). Create a CEO Check-in for Trent for today. Output exactly 4 short lines:\n1) ONE priority\n2) ONE tiny task (10–20 min)\n3) ONE thing to ignore\n4) ONE question if absolutely necessary (otherwise write 'No questions').\nContext: trip planning Apr 9–19 (HK/Osaka/Jeju/Busan), job momentum, wellbeing/sleep. Keep it calm." 2>/dev/null || true)

YOKO=$($OPENCLAW_BIN agent --agent yoko --local --timeout 90 --message "You are Yoko. Rewrite this CEO Check-in to be warmer + crisp, max 6 lines total, keep it actionable and low-pressure. Preserve structure. If it is after 00:30–09:30 quiet hours, add a gentle sleep note; otherwise no. Here is Jaz draft:\n${JAZ}" 2>/dev/null || true)

MSG=$(cat <<EOF
CEO Check‑in · $(date '+%a %d %b')

${YOKO:-$JAZ}
EOF
)

$OPENCLAW_BIN message send --channel telegram --target "$CHAT_TARGET" --message "$MSG" >/dev/null 2>&1 || true
