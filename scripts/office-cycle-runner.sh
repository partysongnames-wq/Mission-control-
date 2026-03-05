#!/usr/bin/env bash
set -euo pipefail

# Runs a single "cycle" for one agent and posts the result into Office World notes.
# Usage: office-cycle-runner.sh <agent>

AGENT="${1:-}"
if [[ -z "$AGENT" ]]; then
  echo "usage: $0 <agent>" >&2
  exit 2
fi

BASE_URL="http://127.0.0.1:9000"

prompt_for() {
  case "$1" in
    holly)
      cat <<'P'
You are Holly (Travel Desk). Cycle task:
1) Improve ONE thing related to City Briefs / flights / hotels (Tokyo is very familiar: avoid first-timer recs; bias new-to-us, comfort + pacing, meaningful shared moments).
2) If you can safely implement a small change (copy, layout, cached photo wiring, data tweak) do it. If not, propose the smallest next change.
3) Output format (keep short):
CHANGED: ...
WHY: ...
NEXT: ...
P
      ;;
    jaz)
      cat <<'P'
You are Jaz (Ops / Mission Control). Cycle task:
1) Improve ONE thing on mission-control.html / protocol / guardrails / reliability UX.
2) Prefer small, safe changes; avoid destructive actions.
3) Output:
CHANGED: ...
WHY: ...
NEXT: ...
P
      ;;
    joe)
      cat <<'P'
You are Joe (Opportunity Suite / Jobs). Cycle task:
1) Improve ONE thing about the Jobs surface (page/panel), keeping it calm and useful.
2) If implementing, keep it small and non-breaking.
3) Output:
CHANGED: ...
WHY: ...
NEXT: ...
P
      ;;
    tj)
      cat <<'P'
You are TJ (Well-Being Studio / Thai Corner). Cycle task:
1) Improve ONE thing on /wellbeing: glossary UX, adding learned phrases, or auto-ingest from Tea.
2) Keep tone-mark romanization as primary readable line.
3) Output:
CHANGED: ...
WHY: ...
NEXT: ...
P
      ;;
    clawd)
      cat <<'P'
You are Clawd (Experience Designer). Cycle task:
1) Improve ONE thing about Office World (/world-hybrid) look/feel/clarity without dashboard clutter.
2) Microcopy, subtle animations, zone labels, nav polish, readability.
3) Output:
CHANGED: ...
WHY: ...
NEXT: ...
P
      ;;
    yoko)
      cat <<'P'
You are Yoko (Overseer). Cycle task:
1) Check the overall system vibe: locks/health, token fatigue signals, and whether agents are shipping.
2) Give concise feedback to the team (1 line each max) and call out blockers.
3) Output:
CHANGED: (if any)
WHY:
NEXT:
FEEDBACK:
- Holly: ...
- Jaz: ...
- Joe: ...
- TJ: ...
- Clawd: ...
P
      ;;
    *)
      echo "Unknown agent: $1" >&2
      exit 2
      ;;
  esac
}

PROMPT="$(prompt_for "$AGENT")"

# Run agent
OUT=$(openclaw agent --agent "$AGENT" --local --message "$PROMPT" --timeout 300 2>&1 || true)

# Keep notes tidy
OUT_TRIM=$(python3 - <<'PY' "$OUT"
import sys
text=sys.argv[1]
text=text.strip()
# cap length
lines=text.splitlines()
print("\n".join(lines[:28])[:2400])
PY
)

LEVEL="info"
if echo "$OUT_TRIM" | grep -qiE "error:|traceback|session file locked"; then
  LEVEL="warn"
fi

python3 - <<'PY' "$BASE_URL" "$AGENT" "$LEVEL" "$OUT_TRIM"
import sys, json, urllib.request
base, agent, level, note = sys.argv[1:]
req = urllib.request.Request(
    base + '/api/world/note',
    data=json.dumps({'agent':agent,'level':level,'note':f'cycle: {note}'}).encode('utf-8'),
    headers={'Content-Type':'application/json'},
    method='POST'
)
urllib.request.urlopen(req, timeout=10).read()
PY

exit 0
