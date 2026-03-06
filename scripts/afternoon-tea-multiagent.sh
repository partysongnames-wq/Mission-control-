#!/usr/bin/env bash
set -euo pipefail

CHAT_TARGET="telegram:-5287663927"
STATE_FILE="$HOME/.""/afternoon-tea-city-rotate.json"

OPENCLAW_BIN="/Users/MacBookAir/.nvm/versions/node/v22.22.0/bin/"""

ask() {
  agent="$1"
  prompt="$2"
  "" agent --agent "$agent" --local --message "$prompt" --timeout 140 2>/dev/null || true
}

# ---- Focus city rotation (from travel-flight-plan.json if present) ----
python3 - <<'PY' "$STATE_FILE" >/tmp/tea_focus.txt
import json, os, sys
from pathlib import Path

state_path = Path(sys.argv[1])

# Preferred rotation list (fallback)
fallback = ["Busan", "Osaka", "Hiroshima", "Jeju", "Hong Kong"]

# Try to load from repo travel-flight-plan.json city_briefs order
repo = Path('/Users/MacBookAir/clawd')
plan = repo / 'travel-flight-plan.json'
cities = []
if plan.exists():
    try:
        data = json.loads(plan.read_text(encoding='utf-8'))
        for item in data.get('city_briefs', []):
            c = item.get('city')
            if c:
                cities.append(c)
    except Exception:
        cities = []

if not cities:
    cities = fallback

state = {"i": 0}
if state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except Exception:
        state = {"i": 0}

i = int(state.get('i', 0)) % len(cities)
focus = cities[i]
state['i'] = (i + 1) % len(cities)
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

print(focus)
PY

FOCUS_CITY=$(cat /tmp/tea_focus.txt | head -n 1 | tr -d '\r' | sed 's/^ *//;s/ *$//')
if [[ -z "$FOCUS_CITY" ]]; then FOCUS_CITY="Busan"; fi

# ---- Round 1: distinct voices (short) ----
TJ=$(ask tj "You are TJ. Thai for today, tied to ${FOCUS_CITY}. 4 lines max: Thai script, tone-mark romanization (hyphenated), meaning, 1 short example sentence.")
HOLLY=$(ask holly "You are Holly. Focus city: ${FOCUS_CITY}. Give ONE plan idea that feels special for Trent + Nasha. 4 lines max: idea + why + 1 practical tip + 1 gentle romantic touch.")
JOE=$(ask joe "You are Joe. Focus city: ${FOCUS_CITY}. Give ONE career angle (networking/coworking/industry) or a quick job move that keeps momentum while traveling. 3 lines max.")
JAZ=$(ask jaz "You are Jaz (chief of staff). Focus city: ${FOCUS_CITY}. Give 3 bullets: what to do, what to avoid, what to decide. Max 5 lines.")
YOKO=$(ask yoko "You are Yoko (ops concierge). Give 3–4 lines max: today's system vibe, 1 risk to watch, 1 suggestion. Avoid jargon.")

# ---- Round 2: crossover (ask each other) ----
# Ask Jaz to react to Holly (pacing/priorities)
CROSS1=$(ask jaz "Holly proposed this for ${FOCUS_CITY}:\n${HOLLY}\n\nReact in 2 lines: (1) keep/change? (2) simplest version.")
# Ask Holly to react to Jaz (make it more human/romantic)
CROSS2=$(ask holly "Jaz said this about ${FOCUS_CITY}:\n${JAZ}\n\nReact in 2 lines: (1) one warm tweak for Trent+Nasha, (2) one comfort/pacing guardrail.")
# Ask Yoko to sanity-check Joe (ops/time/energy)
CROSS3=$(ask yoko "Joe suggested this: \n${JOE}\n\nReact in 2 lines: what to keep, and one risk/constraint to watch.")

# Compose message (<=25 lines)
msg=$(cat <<EOF
Afternoon Tea · $(date '+%a %d %b')
Focus: ${FOCUS_CITY}

TJ — Thai
$TJ

Holly — ${FOCUS_CITY}
$HOLLY

Jaz → Holly
$CROSS1

Holly → Jaz
$CROSS2

Joe — Jobs
$JOE

Yoko → Joe
$CROSS3

Yoko — Ops
$YOKO
EOF
)

# Trim to ~25 lines max
msg=$(python3 - <<'PY' "$msg"
import sys
text=sys.argv[1]
lines=[ln.rstrip() for ln in text.strip().splitlines()]
# drop extra blank lines at end
while lines and lines[-1]=='':
    lines.pop()
print("\n".join(lines[:25]).rstrip())
PY
)

"" message send --channel telegram --target "$CHAT_TARGET" --message "$msg" >/dev/null 2>&1 || true
