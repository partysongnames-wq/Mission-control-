#!/usr/bin/env bash
set -euo pipefail

BASE_URL="http://127.0.0.1:9000"
OUT_STATE="$HOME/.openclaw/oauth-quota-history.json"
PROBE_STATE="$HOME/.openclaw/oauth-probe-state.json"

mkdir -p "$HOME/.openclaw"

# Pull token usage snapshot (context usage per session/agent)
TU_JSON=$(curl -s "$BASE_URL/token-usage" || echo "{}")

# Probe health (best-effort)
OAUTH_OK="unknown"
OAUTH_LAST_FAIL_TS=null
OAUTH_CONSEC_FAIL=null
if [[ -f "$PROBE_STATE" ]]; then
  OAUTH_OK=$(python3 - <<'PY' "$PROBE_STATE"
import json,sys
p=sys.argv[1]
try:
  s=json.load(open(p))
  ok=s.get('oauthOk')
  # legacy keys supported
  if ok is None:
    ok = s.get('ok')
  print('true' if ok is True else 'false' if ok is False else 'unknown')
except Exception:
  print('unknown')
PY
)
  OAUTH_LAST_FAIL_TS=$(python3 - <<'PY' "$PROBE_STATE"
import json,sys
p=sys.argv[1]
try:
  s=json.load(open(p))
  v=s.get('lastFailureAt') or s.get('last_failure_at')
  if v is None: print('null')
  else: print(json.dumps(v))
except Exception:
  print('null')
PY
)
  OAUTH_CONSEC_FAIL=$(python3 - <<'PY' "$PROBE_STATE"
import json,sys
p=sys.argv[1]
try:
  s=json.load(open(p))
  v=s.get('consecutiveFailures') or s.get('consecutive_failures')
  if v is None: print('null')
  else: print(int(v))
except Exception:
  print('null')
PY
)
fi

NOW_ISO=$(python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
print(datetime.now(ZoneInfo('Asia/Bangkok')).isoformat())
PY
)

python3 - <<'PY' "$OUT_STATE" "$NOW_ISO" "$OAUTH_OK" "$OAUTH_LAST_FAIL_TS" "$OAUTH_CONSEC_FAIL" "$TU_JSON"
import json, sys
from pathlib import Path

out_path = Path(sys.argv[1])
ts = sys.argv[2]
oauth_ok = sys.argv[3]
last_fail = json.loads(sys.argv[4]) if sys.argv[4] != 'null' else None
consec = None if sys.argv[5] == 'null' else int(sys.argv[5])

tu = json.loads(sys.argv[6]) if sys.argv[6].strip() else {}

state = {"snapshots": []}
if out_path.exists():
    try:
        state = json.loads(out_path.read_text(encoding='utf-8'))
    except Exception:
        state = {"snapshots": []}

snap = {
    "ts": ts,
    "oauth": {
        "ok": oauth_ok,
        "lastFailureAt": last_fail,
        "consecutiveFailures": consec,
    },
    "tokenUsage": tu,
}

state.setdefault('snapshots', []).append(snap)
# keep last 60 days
state['snapshots'] = state['snapshots'][-60:]

out_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
print('wrote', out_path)
PY
