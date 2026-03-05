#!/usr/bin/env bash
set -euo pipefail

CHAT_TARGET="telegram:-5287663927"

command -v openclaw >/dev/null 2>&1 || exit 0

ask() {
  agent="$1"
  prompt="$2"
  openclaw agent --agent "$agent" --local --message "$prompt" --timeout 120 2>/dev/null || true
}

# Collect contributions (short, distinct voices)
TJ=$(ask tj "You are TJ. Provide Thai word of the day in 4 lines max: Thai script, simple pronunciation, meaning, 1 short example sentence.")
HOLLY=$(ask holly "You are Holly. Give ONE Japan/travel nugget (Nasha-aware): 4 lines max: idea + why + 1 practical tip + 1 gentle romantic touch.")
JOE=$(ask joe "You are Joe. Give ONE small career move for today (high ROI) in 3 lines max: action + why + 1 next step.")
JAZ=$(ask jaz "You are Jaz (chief of staff). Give a 3-bullet 'Afternoon Tea' priorities: 1) Trent, 2) Nasha, 3) Ops. Max 5 lines.")
YOKO=$(ask yoko "You are Yoko (ops concierge). Give OpenClaw ops note in 4 lines max: status, 1 risk to watch, 1 suggestion. Avoid jargon.")

# Compose message (<=25 lines)
msg=$(cat <<EOF
Afternoon Tea · $(date '+%a %d %b')

TJ — Thai word
$TJ

Holly — Travel nugget
$HOLLY

Joe — Jobs
$JOE

Jaz — Priorities
$JAZ

Yoko — Ops
$YOKO
EOF
)

# Trim to ~25 lines max
msg=$(python3 - <<'PY' "$msg"
import sys
text=sys.argv[1]
lines=text.strip().splitlines()
# keep at most 25 lines
print("\n".join(lines[:25]).rstrip())
PY
)

openclaw message send --channel telegram --target "$CHAT_TARGET" --message "$msg" >/dev/null 2>&1 || true
