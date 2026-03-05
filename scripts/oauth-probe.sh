#!/usr/bin/env bash
set -euo pipefail

# OAuth refresh smoke test for openai-codex via a tiny agent turn.
# Alerts Telegram only after 2 consecutive failures.

STATE_FILE="$HOME/.openclaw/oauth-probe-state.json"
CHAT_TARGET="telegram:-5287663927"
AGENT_ID="yoko"

command -v openclaw >/dev/null 2>&1 || exit 0

mkdir -p "$(dirname "$STATE_FILE")"
if [[ ! -f "$STATE_FILE" ]]; then
  echo '{"consecutiveFails":0,"lastAlertAt":null}' > "$STATE_FILE"
fi

# Run a tiny turn. Use low timeout.
out=""
err=""
code=0
set +e
out=$(openclaw agent --agent "$AGENT_ID" --local --message "oauth probe: reply OK" --timeout 30 2>&1)
code=$?
set -e

python3 - <<'PY' "$STATE_FILE" "$CHAT_TARGET" "$code" "$out"
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import subprocess

state_path, target, code_s, output = sys.argv[1:]
code = int(code_s)

try:
    state = json.loads(open(state_path,'r',encoding='utf-8').read() or '{}')
except Exception:
    state = {"consecutiveFails": 0, "lastAlertAt": None}

now = datetime.now(ZoneInfo('Asia/Bangkok'))

def send(msg: str):
    try:
        subprocess.run([
            'openclaw','message','send',
            '--channel','telegram',
            '--target', target,
            '--message', msg,
        ], capture_output=True, text=True, timeout=15)
    except Exception:
        pass

ok = (code == 0) and ('OK' in output)

if ok:
    state['consecutiveFails'] = 0
    open(state_path,'w',encoding='utf-8').write(json.dumps(state, indent=2))
    sys.exit(0)

# failure
state['consecutiveFails'] = int(state.get('consecutiveFails', 0)) + 1

# alert only after 2 consecutive failures
if state['consecutiveFails'] >= 2:
    last = state.get('lastAlertAt')
    # avoid spamming: at most one alert per hour
    should = True
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < 3600:
                should = False
        except Exception:
            pass

    if should:
        snippet = output.replace('\n',' ')[:240]
        msg = (
            "[OAuth Probe] openai-codex refresh looks broken (2+ consecutive failures).\n"
            f"Agent: {sys.argv[3]} (yoko)\n"
            f"Error snippet: {snippet}\n\n"
            "Fix: re-auth OpenAI-codex on the laptop, then check /health for stale locks."
        )
        send(msg)
        state['lastAlertAt'] = now.isoformat()

open(state_path,'w',encoding='utf-8').write(json.dumps(state, indent=2))
PY
