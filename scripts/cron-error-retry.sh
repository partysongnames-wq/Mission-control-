#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="$HOME/.openclaw/cron-retry-state.json"
CHAT_ID="telegram:-5287663927"

command -v openclaw >/dev/null 2>&1 || exit 0

mkdir -p "$(dirname "$STATE_FILE")"

# Load state (jobId -> {date, retriedCount})
python3 - <<'PY' "$STATE_FILE"
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
if p.exists():
    try:
        json.load(p.open())
    except Exception:
        p.write_text("{}")
else:
    p.write_text("{}")
PY

# Get cron list as text; parse error rows by columns (ID ... Status)
# We will also use openclaw cron list --json if available; if not, parse the table.
json=$(openclaw cron list --json 2>/dev/null || true)

python3 - <<'PY' "$STATE_FILE" "$json" "$CHAT_ID"
import json, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import subprocess

state_path = Path(sys.argv[1])
raw_json = sys.argv[2]
chat_id = sys.argv[3]

today = datetime.now(ZoneInfo('Asia/Bangkok')).date().isoformat()

state = {}
try:
    state = json.loads(state_path.read_text() or '{}')
except Exception:
    state = {}

try:
    data = json.loads(raw_json) if raw_json.strip() else None
except Exception:
    data = None

jobs = []
if isinstance(data, dict) and 'jobs' in data:
    jobs = data['jobs'] or []
else:
    # fallback: parse table output
    out = subprocess.run(['openclaw','cron','list'], capture_output=True, text=True).stdout.splitlines()
    # Skip header line(s)
    for line in out:
        if not line.strip() or line.strip().startswith('ID'):
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        job_id = parts[0]
        status = parts[6]
        name = ' '.join(parts[1:2])
        jobs.append({'id': job_id, 'status': status, 'name': name})

# We will retry only jobs whose status == 'error'
errors = [j for j in jobs if (j.get('status') == 'error')]

alerts = []

for j in errors:
    job_id = j.get('id') or j.get('jobId')
    name = j.get('name') or j.get('Name') or 'Cron'
    if not job_id:
        continue

    rec = state.get(job_id) or {'date': today, 'retried': 0, 'failedTwice': 0}
    if rec.get('date') != today:
        rec = {'date': today, 'retried': 0, 'failedTwice': 0}

    # If we already retried once today, do not keep retrying.
    if int(rec.get('retried', 0)) >= 1:
        # If not already flagged failedTwice, check last run status and mark/alert.
        if not rec.get('failedTwice'):
            runs = subprocess.run(['openclaw','cron','runs','--id',job_id,'--limit','1'], capture_output=True, text=True).stdout
            try:
                r = json.loads(runs)
                ent = (r.get('entries') or [{}])[0]
                if ent.get('status') == 'error':
                    rec['failedTwice'] = 1
                    alerts.append(f"Cron still failing after retry: {name} ({job_id})")
            except Exception:
                pass
        state[job_id] = rec
        continue

    # Retry once
    subprocess.run(['openclaw','cron','run',job_id,'--expect-final','--timeout','600000'], capture_output=True, text=True)
    rec['retried'] = int(rec.get('retried',0)) + 1

    # Re-check last run
    runs = subprocess.run(['openclaw','cron','runs','--id',job_id,'--limit','1'], capture_output=True, text=True).stdout
    try:
        r = json.loads(runs)
        ent = (r.get('entries') or [{}])[0]
        if ent.get('status') == 'error':
            rec['failedTwice'] = 1
            alerts.append(f"Cron failed after retry: {name} ({job_id})")
    except Exception:
        pass

    state[job_id] = rec

# Save state
state_path.write_text(json.dumps(state, indent=2))

# If alerts, push into world state so Yoko lights up (Telegram alert can be added later)
for msg in alerts[:5]:
    payload = json.dumps({'agent':'yoko','note':msg,'level':'error'})
    try:
        subprocess.run(['curl','-s','-X','POST','http://127.0.0.1:9000/api/world/note','-H','Content-Type: application/json','-d',payload],
                       capture_output=True, text=True, timeout=4)
    except Exception:
        pass
PY
