#!/usr/bin/env bash
set -euo pipefail

JOB_ID="1ee2f083-5961-407a-8ed5-ad0dcc74103e"

# If openclaw isn't available, bail quietly.
command -v openclaw >/dev/null 2>&1 || exit 0

# Fetch last run (JSON)
json=$(openclaw cron runs --id "$JOB_ID" --limit 1 2>/dev/null || true)

# If no history, run once.
if [[ -z "$json" ]]; then
  openclaw cron run "$JOB_ID" --expect-final --timeout 600000 >/dev/null 2>&1 || true
  exit 0
fi

python3 - <<'PY' "$JOB_ID" "$json"
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo

job_id = sys.argv[1]
raw = sys.argv[2]

try:
    data = json.loads(raw)
except Exception:
    # can't parse => best-effort run
    sys.exit(2)

entries = data.get('entries') or []
if not entries:
    sys.exit(2)

last_ts_ms = entries[0].get('ts')
if not isinstance(last_ts_ms, int):
    sys.exit(2)

now = datetime.now(ZoneInfo('Asia/Bangkok')).date()
last = datetime.fromtimestamp(last_ts_ms/1000, ZoneInfo('Asia/Bangkok')).date()

# Exit 0 means "already ran today"
# Exit 3 means "needs catch-up"
if last == now:
    sys.exit(0)
else:
    sys.exit(3)
PY

code=$?
if [[ $code -eq 0 ]]; then
  exit 0
fi

# code 2 (parse fail) or 3 (needs catch-up) => run once
openclaw cron run "$JOB_ID" --expect-final --timeout 600000 >/dev/null 2>&1 || true
