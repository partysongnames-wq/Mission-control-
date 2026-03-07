"""Mission Control helper server."""
import json
import os
import shlex
import signal
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional

from flask import Flask, abort, jsonify, redirect, send_from_directory, request

BASE_DIR = Path(__file__).parent.resolve()

WORLD_STATE_PATH = BASE_DIR / 'mission-control-world-state.json'
WORLD_STATE_LOCK = threading.Lock()

WELLBEING_STATE_PATH = BASE_DIR / 'mission-control-wellbeing-state.json'
WELLBEING_LOCK = threading.Lock()


def _load_wellbeing() -> dict:
    with WELLBEING_LOCK:
        if WELLBEING_STATE_PATH.exists():
            try:
                return json.loads(WELLBEING_STATE_PATH.read_text(encoding='utf-8'))
            except Exception:
                return {'thai': {'words': [], 'wordOfDay': None}}
        return {'thai': {'words': [], 'wordOfDay': None}}


def _save_wellbeing(state: dict) -> None:
    with WELLBEING_LOCK:
        WELLBEING_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


WORLD_BOUNDS = {
    'minX': 0.08,
    'maxX': 0.92,
    'minY': 0.14,
    'maxY': 0.90,
}
WORLD_MAX_MOVES_PER_DAY = 5

# Quiet hours (Bangkok) — gentle nudge for non-urgent work
QUIET_HOURS_START = (0, 30)  # 00:30
QUIET_HOURS_END = (9, 30)    # 09:30

WORLD_CHAT_LIMIT_PER_HOUR = 3
WORLD_CHAT_MAX_LEN = 180

WORLD_ZONES = {
    # normalized x/y in the world room
    'office_door': {'x': 0.88, 'y': 0.50},
    'intray': {'x': 0.92, 'y': 0.56},
    'health_corner': {'x': 0.92, 'y': 0.88},
    'creative_corner': {'x': 0.86, 'y': 0.18},
}


APPROVAL_KEYWORDS = [
    'approve', 'approval', 'should i', 'can i', 'ok to',
    'book', 'booking', 'buy', 'purchase', 'spend', 'pay',
    'delete', 'remove', 'terminate', 'kill', 'release all',
]





def _bkk_date_str() -> str:
    return datetime.now(ZoneInfo('Asia/Bangkok')).date().isoformat()


def _load_world_state() -> Dict:
    with WORLD_STATE_LOCK:
        if WORLD_STATE_PATH.exists():
            try:
                return json.loads(WORLD_STATE_PATH.read_text(encoding='utf-8'))
            except Exception:
                return {'notes': {}, 'unread': {}, 'positions': {}, 'moves': {}, 'chat': [], 'chatApprovals': {}, 'chatCounters': {}}
        return {'notes': {}, 'unread': {}, 'positions': {}, 'moves': {}, 'chat': [], 'chatApprovals': {}, 'chatCounters': {}}


def _world_prune(state: Dict) -> Dict:
    """Prune ephemeral office chat (>24h) and expired approvals."""
    try:
        now = datetime.now(ZoneInfo('Asia/Bangkok'))
        # chat items stored with iso ts
        chat = state.get('chat') or []
        kept = []
        for c in chat:
            ts = c.get('ts')
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                continue
            if (now - dt).total_seconds() <= 24*3600:
                kept.append(c)
        state['chat'] = kept[-200:]

        # approvals
        appr = state.get('chatApprovals') or {}
        new_appr = {}
        for token, a in appr.items():
            try:
                exp = datetime.fromisoformat(a.get('expiresAt'))
                if exp > now:
                    new_appr[token] = a
            except Exception:
                continue
        state['chatApprovals'] = new_appr
    except Exception:
        pass
    return state


def _save_world_state(state: Dict) -> None:
    state = _world_prune(state)
    with WORLD_STATE_LOCK:
        WORLD_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def _world_add_note(agent: str, note: str, level: str = 'info') -> Dict:
    state = _load_world_state()
    state.setdefault('notes', {})
    state.setdefault('unread', {})
    state['notes'].setdefault(agent, [])
    ts = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
    state['notes'][agent].insert(0, {'ts': ts, 'note': note, 'level': level})
    state['notes'][agent] = state['notes'][agent][:30]
    state['unread'][agent] = int(state['unread'].get(agent, 0)) + 1
    _save_world_state(state)
    return state




def _in_quiet_hours(now=None) -> bool:
    try:
        now = now or datetime.now(ZoneInfo('Asia/Bangkok'))
    except Exception:
        return False
    start = now.replace(hour=QUIET_HOURS_START[0], minute=QUIET_HOURS_START[1], second=0, microsecond=0)
    end = now.replace(hour=QUIET_HOURS_END[0], minute=QUIET_HOURS_END[1], second=0, microsecond=0)
    # Quiet window is same-day (00:30 -> 09:30)
    return start <= now <= end


def _is_urgent_text(txt: str) -> bool:
    t = (txt or '').lower()
    urgent_markers = ['urgent', 'oauth', 'session file locked', 'lock', 'cannot send', 'down', 'error', 'failed', 'timeout']
    return any(m in t for m in urgent_markers)

def _send_telegram_message(text_msg: str) -> None:
    # Best-effort: send to the Open Claw Optimization Telegram group
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', 'telegram:-5287663927',
            '--message', text_msg,
        ], capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _world_set_position(agent: str, x: float, y: float) -> Dict:
    state = _load_world_state()
    state.setdefault('positions', {})
    state.setdefault('moves', {})

    # clamp
    x = max(WORLD_BOUNDS['minX'], min(WORLD_BOUNDS['maxX'], float(x)))
    y = max(WORLD_BOUNDS['minY'], min(WORLD_BOUNDS['maxY'], float(y)))

    state['positions'][agent] = {
        'x': x,
        'y': y,
        'updatedAt': datetime.now(ZoneInfo('Asia/Bangkok')).isoformat(),
    }
    _save_world_state(state)
    return state


def _world_clear_unread(agent: str) -> Dict:
    state = _load_world_state()
    state.setdefault('unread', {})
    state['unread'][agent] = 0
    _save_world_state(state)
    return state

app = Flask(__name__, static_folder=None)

COMMANDS: Dict[str, str] = {
    "travel": "openclaw agent --local --agent super-jobs --message \"Use Topic Monitor to watch Bangkok to Japan fares and surface the cheapest legs.\"",
    "manifesto": "openclaw agent --local --agent super-jobs --message \"Use morning-manifesto to plan tomorrow's top 3 travel actions.\"",
    "memory": "openclaw agent --local --agent super-jobs --message \"Use supermemory to store: [paste your note here]\"",
    "token": "openclaw cron list",
    "watch": "openclaw agent --local --agent super-jobs --message \"Refresh travel alert for Bangkok to Japan routes.\"",
    "pricewatch": "openclaw cron run d39d96cc-b37b-4cd6-9ff9-2c0bc2b39c9e --expect-final --timeout 600000",
    "deals": "bash -lc \"openclaw cron run d39d96cc-b37b-4cd6-9ff9-2c0bc2b39c9e --expect-final --timeout 600000 && /Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python deals_radar.py\"",
    "hotelwatch": "bash -lc \"/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python hotel_watch.py\"",
}

AGENT_DISPATCH: Dict[str, str] = {
    # Map lounge avatar key -> OpenClaw agent id
    'yoko': 'yoko',
    'jaz': 'jaz',
    'tj': 'tj',
    'holly': 'holly',
    'joe': 'joe',
    'clawd': 'clawd',
}

statuses = {
    "travel": {"label": "Travel Scan", "state": "idle", "detail": "Last run: none"},
    "manifesto": {"label": "Morning Manifesto", "state": "idle", "detail": "Last run: none"},
    "memory": {"label": "Memory Snapshot", "state": "idle", "detail": "Last run: none"},
    "token": {"label": "Token Pulse", "state": "idle", "detail": "Last run: none"},
    "watch": {"label": "Travel Watch", "state": "idle", "detail": "Last run: none"},
    "pricewatch": {"label": "Price Check Now", "state": "idle", "detail": "Last run: none"},
    "deals": {"label": "Deals Radar", "state": "idle", "detail": "Last run: none"},
    "hotelwatch": {"label": "Hotel Check Now", "state": "idle", "detail": "Last run: none"},
}

HTML_PAGES = {p.name for p in BASE_DIR.glob("*.html")}
JSON_FILES = {p.name for p in BASE_DIR.glob("*.json")}

GROUP_LABELS = {
    "-5275095885": "Clawd Skills (current group)",
    "-5147090783": "Clawd Skills (token board)",
    "-5158792881": "Dinner planner (g-dinner-planner)",
    "-5046650111": "Video / creative ops",
    "-5000702054": "Grok experimentation",
    "-5276342051": "Off-topic test chat",
}


@app.route("/assets/<path:filename>")
def serve_asset(filename: str):
    asset_path = BASE_DIR / "assets"
    if not (asset_path / filename).exists():
        abort(404)
    return send_from_directory(asset_path, filename)


def _fetch_sessions_json() -> List[Dict]:
    try:
        proc = subprocess.run(
            ['/Users/MacBookAir/.nvm/versions/node/v22.22.0/bin/openclaw', 'sessions', '--all-agents', '--json'],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []
    return data.get("sessions", [])


def fetch_token_usage() -> List[Dict]:
    sessions = _fetch_sessions_json()
    usage = []
    for session in sessions:
        total = session.get("totalTokens")
        limit = session.get("contextTokens")
        if not isinstance(total, (int, float)) or not limit:
            continue
        percent = min(total / limit, 1.0)
        key = session.get("key", "unknown")
        suffix = key.split(":")[-1]
        group_hint = GROUP_LABELS.get(suffix, suffix)
        kind = session.get("kind") or "unknown"
        agent_id = session.get("agentId") or "agent"
        label = f"{agent_id} · {kind} · {group_hint}"
        usage.append(
            {
                "key": key,
                "agentId": agent_id,
                "kind": kind,
                "model": session.get("model"),
                "used": total,
                "limit": limit,
                "percent": round(percent, 4),
                "label": label,
                "groupSuffix": suffix,
            }
        )
    usage.sort(key=lambda entry: entry["percent"], reverse=True)
    return usage[:200]


def set_status(action: str, state: str, detail: str) -> None:
    if action in statuses:
        statuses[action]["state"] = state
        statuses[action]["detail"] = detail


world_action_map = {
    'travel': 'tj',
    'manifesto': 'jaz',
    'watch': 'yoko',
    'memory': 'holly',
    'sidejobs': 'joe',
    'clawd': 'clawd',
    'pricewatch': 'yoko',
    'deals': 'yoko',
}


@app.route("/run/<action>")
def run_action(action: str):
    command = COMMANDS.get(action)
    if not command:
        return jsonify(success=False, message="Unknown action"), HTTPStatus.NOT_FOUND
    set_status(action, "running", "Working…")
    proc = subprocess.run(shlex.split(command), cwd=str(BASE_DIR), capture_output=True, text=True)
    output = proc.stdout.strip() or proc.stderr.strip() or "Done."
    detail = output.replace("\n", " ")[:120]
    set_status(action, "idle", f"Last run: {detail}")
    try:
        agent_key = world_action_map.get(action)
        if agent_key and proc.returncode == 0:
            _world_add_note(agent_key, f"{action}: {detail}", level='info')
    except Exception:
        pass    # If we just ran a price check, stamp last-checked + compute deltas + append history.
    if action == 'pricewatch' and proc.returncode == 0:
        try:
            plan_path = BASE_DIR / 'travel-flight-plan.json'
            if plan_path.exists():
                before = json.loads(plan_path.read_text(encoding='utf-8'))
                # After cron run may have updated the JSON; reload it.
                after = json.loads(plan_path.read_text(encoding='utf-8'))
                after.setdefault('meta', {})
                ts = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
                after['meta']['prices_last_checked'] = ts
                after['meta'].setdefault('prices_source', 'Skyscanner')

                # Build flight index by id to compare.
                bmap = {f.get('id'): f for f in (before.get('flights') or [])}
                changes = []
                for f in (after.get('flights') or []):
                    fid = f.get('id')
                    if not fid:
                        continue
                    old = bmap.get(fid, {})
                    oldp = old.get('price_per_person')
                    newp = f.get('price_per_person')
                    if isinstance(oldp, (int, float)) and isinstance(newp, (int, float)) and oldp != newp:
                        changes.append({
                            'id': fid,
                            'leg': f"{f.get('from_code','?')}→{f.get('to_code','?')}",
                            'old': oldp,
                            'new': newp,
                            'delta': newp - oldp,
                        })

                    # Append price history point (best effort).
                    f.setdefault('price_history', [])
                    if isinstance(newp, (int, float)) and newp > 0:
                        f['price_history'].append({'ts': ts, 'price_per_person': newp})
                        f['price_history'] = f['price_history'][-60:]

                if changes:
                    after['meta']['prices_last_result'] = f"Updated ({len(changes)} legs changed)"
                    after['meta']['prices_last_changes'] = changes
                else:
                    after['meta']['prices_last_result'] = 'Checked — no material price changes found.'
                    after['meta']['prices_last_changes'] = []

                plan_path.write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
    return jsonify(success=proc.returncode == 0, message=output)


@app.route("/status")
def status():
    return jsonify(statuses)


@app.route("/token-usage")
def token_usage_page():
    usage = fetch_token_usage()
    return jsonify({"sessions": usage})


@app.route("/flight-plan.ics")
def flight_plan_ics():
    """Generate an ICS calendar from travel-flight-plan.json."""
    plan_path = BASE_DIR / "travel-flight-plan.json"
    if not plan_path.exists():
        abort(404)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        abort(500)

    flights = plan.get("flights", [])

    year = 2026
    month_lookup = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }

    def parse_day_month(s: str):
        s = (s or "").strip()
        s = s.split("or")[0].strip()
        parts = s.split()
        if len(parts) < 2:
            return None
        try:
            day = int(parts[0])
            mon = month_lookup.get(parts[1][:3])
            if not mon:
                return None
            return (year, mon, day)
        except ValueError:
            return None

    def dtfmt(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenClaw//Mission Control Flight Plan//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    now = datetime.utcnow()

    for f in flights:
        ymd = parse_day_month(f.get("date", ""))
        depart = (f.get("depart") or "00:00").strip()
        arrive = (f.get("arrive") or "00:00").strip()
        if not ymd:
            continue
        try:
            dh, dm = [int(x) for x in depart.split(":")[:2]]
            ah, am = [int(x) for x in arrive.split(":")[:2]]
        except Exception:
            continue

        start = datetime(ymd[0], ymd[1], ymd[2], dh, dm)
        end = datetime(ymd[0], ymd[1], ymd[2], ah, am)
        if f.get("next_day") or end <= start:
            end = end + timedelta(days=1)

        uid = f"{f.get('id','flight')}@mission-control"
        summary = f"{f.get('from_code')}→{f.get('to_code')} {f.get('airline')} {f.get('flight_number')}"
        desc = (f.get("notes") or "").replace("\n", " ")[:900]

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtfmt(now)}Z",
            f"DTSTART:{dtfmt(start)}",
            f"DTEND:{dtfmt(end)}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    ics = "\r\n".join(lines) + "\r\n"
    return app.response_class(ics, mimetype="text/calendar; charset=utf-8")




def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True

def _inspect_pid(pid: int) -> Dict:
    """Return basic process info for a PID via ps (best-effort)."""
    info = {"pid": pid, "etime": None, "command": None}
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime=,command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and out:
            # First token is elapsed, remainder is command
            parts = out.split(maxsplit=1)
            if parts:
                info["etime"] = parts[0]
            if len(parts) > 1:
                info["command"] = parts[1]
    except Exception:
        pass
    return info



def _collect_lock_files() -> List[Dict]:
    """Collect *.lock files under ~/.openclaw/agents/*/sessions/."""
    root = Path.home() / ".openclaw" / "agents"
    locks: List[Dict] = []
    now = datetime.utcnow()
    if not root.exists():
        return locks
    for agent_dir in sorted(root.glob("*")):
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for lock_path in sorted(sessions_dir.glob("*.lock")):
            pid = None
            created_at = None
            try:
                payload = json.loads(lock_path.read_text(encoding="utf-8"))
                pid = payload.get("pid")
                created_at = payload.get("createdAt")
            except Exception:
                payload = {}
            alive = bool(pid) and isinstance(pid, int) and _pid_alive(pid)
            proc_info = _inspect_pid(pid) if (pid and isinstance(pid, int)) else {"pid": pid, "etime": None, "command": None}
            age_seconds = None
            if created_at:
                try:
                    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age_seconds = (now - ts.replace(tzinfo=None)).total_seconds()
                except Exception:
                    age_seconds = None
            locks.append({
                "agent": agent_dir.name,
                "lockName": lock_path.name,
                "path": str(lock_path),
                "pid": pid,
                "createdAt": created_at,
                "pidAlive": alive,
                "ageSeconds": age_seconds,
                "procInfo": proc_info,
            })
    locks.sort(key=lambda x: (x.get("pidAlive", False), -(x.get("ageSeconds") or 0)))
    return locks



@app.route('/api/quiet-hours')
def api_quiet_hours():
    now = datetime.now(ZoneInfo('Asia/Bangkok'))
    return jsonify({
        'now': now.isoformat(),
        'inQuietHours': _in_quiet_hours(now),
        'start': {'hour': QUIET_HOURS_START[0], 'minute': QUIET_HOURS_START[1]},
        'end': {'hour': QUIET_HOURS_END[0], 'minute': QUIET_HOURS_END[1]},
    })


@app.route('/api/world/state')
def api_world_state():
    return jsonify(_load_world_state())


@app.route('/api/world/note', methods=['POST'])
def api_world_note():
    payload = request.get_json(silent=True) or {}
    agent = (payload.get('agent') or '').strip()
    note = (payload.get('note') or '').strip()
    level = (payload.get('level') or 'info').strip()
    if not agent or not note:
        return jsonify(success=False, message='agent and note required'), HTTPStatus.BAD_REQUEST
    state = _world_add_note(agent, note, level)

    # auto-walk based on intent
    try:
        nlow = note.lower()
        level_norm = level.lower()
        needs_approval = (level_norm in ['needs_approval','approval']) or any(k in nlow for k in APPROVAL_KEYWORDS)
        if needs_approval:
            z = WORLD_ZONES['office_door']
            _world_set_position(agent, z['x'], z['y'])
        elif level_norm in ['error','warn','warning']:
            z = WORLD_ZONES['health_corner']
            _world_set_position(agent, z['x'], z['y'])
        elif level_norm in ['answer','info','ok','done']:
            # send back to their preferred corner if known (e.g. clawd creative); otherwise do nothing
            if agent == 'clawd':
                z = WORLD_ZONES['creative_corner']
                _world_set_position(agent, z['x'], z['y'])
    except Exception:
        pass

    return jsonify(success=True, state=state)


@app.route('/api/world/ask', methods=['POST'])
def api_world_ask():
    payload = request.get_json(silent=True) or {}
    agent = (payload.get('agent') or '').strip()
    message = (payload.get('message') or '').strip()
    if not agent or not message:
        return jsonify(success=False, message='agent and message required'), HTTPStatus.BAD_REQUEST

    target_agent = AGENT_DISPATCH.get(agent)
    if not target_agent:
        return jsonify(success=False, message=f"unknown agent '{agent}'. Valid: {', '.join(sorted(AGENT_DISPATCH.keys()))}"), HTTPStatus.NOT_FOUND

    # Persist the question as a note (unread)
    try:
        _world_add_note(agent, f"Q: {message}", level='question')
    except Exception:
        pass

    def _runner():
        cmd = ["openclaw", "agent", "--local", "--agent", target_agent, "--message", message]
        try:
            proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=900)
            out = (proc.stdout or '').strip()
            err = (proc.stderr or '').strip()
            combined = out or err or 'Done.'
            snippet = combined.replace("\n", " ")[:220]
            if proc.returncode == 0:
                _world_add_note(agent, snippet, level='answer')
                _send_telegram_message(f"[{agent}] {snippet}")
            else:
                _world_add_note(agent, f"Error: {snippet}", level='error')
                _send_telegram_message(f"[{agent}] Error: {snippet}")
        except subprocess.TimeoutExpired:
            try:
                _world_add_note(agent, "Timed out waiting for agent response.", level='error')
                _send_telegram_message(f"[{agent}] Error: timed out waiting for response")
            except Exception:
                pass
        except Exception as e:
            try:
                _world_add_note(agent, f"Error: {e}", level='error')
            except Exception:
                pass

    try:
        threading.Thread(target=_runner, daemon=True).start()
    except Exception:
        return jsonify(success=False, message='Failed to start agent runner'), HTTPStatus.INTERNAL_SERVER_ERROR

    return jsonify(success=True, message='Queued'), HTTPStatus.ACCEPTED


@app.route('/api/world/move', methods=['POST'])
def api_world_move():
    payload = request.get_json(silent=True) or {}
    agent = (payload.get('agent') or '').strip()
    x = payload.get('x')
    y = payload.get('y')

    if not agent:
        return jsonify(success=False, message='agent required'), HTTPStatus.BAD_REQUEST
    if agent == 'trent':
        return jsonify(success=False, message='director cannot be moved'), HTTPStatus.BAD_REQUEST

    # allow moving only known lounge agents
    if agent not in AGENT_DISPATCH:
        return jsonify(success=False, message=f"unknown agent '{agent}'"), HTTPStatus.NOT_FOUND

    try:
        x = float(x)
        y = float(y)
    except Exception:
        return jsonify(success=False, message='x and y must be numbers (0..1)'), HTTPStatus.BAD_REQUEST

    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return jsonify(success=False, message='x and y must be within 0..1'), HTTPStatus.BAD_REQUEST

    # clamp to office bounds
    x = max(WORLD_BOUNDS['minX'], min(WORLD_BOUNDS['maxX'], x))
    y = max(WORLD_BOUNDS['minY'], min(WORLD_BOUNDS['maxY'], y))

    state = _load_world_state()
    state.setdefault('positions', {})
    state.setdefault('moves', {})

    today = _bkk_date_str()
    m = state['moves'].get(agent) or {}
    if m.get('date') != today:
        m = {'date': today, 'count': 0}

    if int(m.get('count', 0)) >= WORLD_MAX_MOVES_PER_DAY:
        return jsonify(success=False, message=f'move limit reached ({WORLD_MAX_MOVES_PER_DAY}/day)'), HTTPStatus.TOO_MANY_REQUESTS

    m['count'] = int(m.get('count', 0)) + 1
    state['moves'][agent] = m

    state['positions'][agent] = {
        'x': x,
        'y': y,
        'updatedAt': datetime.now(ZoneInfo('Asia/Bangkok')).isoformat(),
    }

    _save_world_state(state)
    return jsonify(success=True, state=state)


@app.route('/api/wellbeing/state')
def api_wellbeing_state():
    return jsonify(_load_wellbeing())


@app.route('/api/wellbeing/thai/add', methods=['POST'])
def api_wellbeing_thai_add():
    payload = request.get_json(silent=True) or {}
    thai = (payload.get('thai') or '').strip()
    roman = (payload.get('roman') or '').strip()
    meaning = (payload.get('meaning') or '').strip()
    example = (payload.get('example') or '').strip()
    if not thai or not roman or not meaning:
        return jsonify(success=False, message='thai, roman, meaning required'), HTTPStatus.BAD_REQUEST
    state = _load_wellbeing()
    state.setdefault('thai', {})
    state['thai'].setdefault('words', [])
    entry = {
        'thai': thai,
        'roman': roman,
        'meaning': meaning,
        'example': example or None,
        'ts': datetime.now(ZoneInfo('Asia/Bangkok')).isoformat(),
    }
    state['thai']['words'].append(entry)
    # keep last 500
    state['thai']['words'] = state['thai']['words'][-500:]
    # also update word of day to the latest added
    state['thai']['wordOfDay'] = entry
    _save_wellbeing(state)
    return jsonify(success=True, state=state)


@app.route('/wellbeing')
def wellbeing_page():
    return redirect('/mission-control-wellbeing.html')



@app.route('/api/cooler/run', methods=['POST'])
def api_cooler_run():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get('topic') or 'Water Cooler').strip()[:80]

    def _run():
        try:
            focus = topic
            people = ['holly', 'jaz', 'joe', 'tj', 'clawd']

            def ask(agent: str, prompt: str, timeout: int = 140) -> str:
                # Use OpenClaw CLI; keep it robust in launchd-like envs.
                oc = '/Users/MacBookAir/.nvm/versions/node/v22.22.0/bin/openclaw'
                cmd = [oc, 'agent', '--agent', agent, '--local', '--message', prompt, '--timeout', str(timeout)]
                return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30).stdout.strip()

            r1 = {}
            for a in people:
                r1[a] = ask(a, f"You are {a}. Water Cooler topic: {focus}. Be friendly + smart. 2-4 lines. One useful idea, plus optional dry/witty aside (no forced jokes). If missing key info, ask ONE question.")

            # Post each person's line into Office World notes so speech bubbles can appear client-side
            for a in people:
                try:
                    _world_note(a, 'cooler: ' + (r1.get(a) or ''), level='answer')
                except Exception:
                    pass

            # Round 2: light crossover questioning
            cross = {}
            cross['jaz->holly'] = ask('jaz', f"Holly said:\n{r1.get('holly', '')}\n\nGive a friendly 1-2 line challenge (dry/witty ok): what's the one assumption? what's the tiny tweak that makes it better?")
            cross['clawd->joe'] = ask('clawd', f"Joe said:\n{r1.get('joe', '')}\n\n1-2 lines: make it more human + less clutter. Add a tasteful joke if it naturally fits.")
            cross['tj->jaz'] = ask('tj', f"Jaz said:\n{r1.get('jaz', '')}\n\n1-2 lines: add a gentle pacing/wellbeing tweak. Friendly tone; one light joke only if natural.")


            try:
                _world_note('yoko', 'cooler: cross-checks ready', level='info')
            except Exception:
                pass

            # Yoko synthesis
            yoko_prompt = (
                "You are Yoko. Write a Telegram summary that feels like a smart friend recapping a quick, playful Water Cooler chat (dry/witty but not snarky). Keep it <= ~35 lines and <= 5 min read:\n"
                "- Best bits (3-6 bullets) with who said it\n"
                "- The one good disagreement (1-2 lines): who challenged what + the improved version\n"
                "- Decisions Trent needs (if any)\n"
                "- Next moves (1 short line per person)\n\n"
                f"Topic: {focus}\n\n"
                f"Round1:\nHolly: {r1.get('holly','')}\nJaz: {r1.get('jaz','')}\nJoe: {r1.get('joe','')}\nTJ: {r1.get('tj','')}\nClawd: {r1.get('clawd','')}\n\n"
                f"Crossover:\nJaz->Holly: {cross.get('jaz->holly','')}\nClawd->Joe: {cross.get('clawd->joe','')}\nTJ->Jaz: {cross.get('tj->jaz','')}\n"
            )
            summary = ask('yoko', yoko_prompt, timeout=180)

            # Post summary into Office Chat + Telegram
            try:
                _world_chat_post('yoko', None, 'water-cooler', f"{focus}:\n" + ((summary[:1800] + '…') if summary and len(summary) > 1800 else (summary or 'done')))
            except Exception:
                pass

            _send_telegram_message(f"Water Cooler · {focus}\n\n{summary or '(no summary generated)'}")

        except Exception as e:
            try:
                _world_note('yoko', f"cooler: error: {e}", level='error')
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(success=True, started=True, topic=topic)

@app.route('/api/tea/run', methods=['POST'])
def api_tea_run():
    # Run the multi-agent tea script in the background.
    script_path = str(BASE_DIR / 'scripts' / 'afternoon-tea-multiagent.sh')

    def _runner():
        try:
            _send_telegram_message('[Tea Party] Meeting started (manual trigger).')
        except Exception:
            pass
        try:
            subprocess.run(['/bin/bash', script_path], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=900)
        except Exception as e:
            try:
                _send_telegram_message(f'[Tea Party] Error running tea: {e}')
            except Exception:
                pass

    try:
        threading.Thread(target=_runner, daemon=True).start()
    except Exception:
        return jsonify(success=False, message='Failed to start tea runner'), HTTPStatus.INTERNAL_SERVER_ERROR

    return jsonify(success=True, message='Tea party triggered'), HTTPStatus.ACCEPTED


@app.route('/api/world/chat', methods=['GET'])
def api_world_chat_get():
    state = _load_world_state()
    state = _world_prune(state)
    return jsonify({
        'chat': state.get('chat') or [],
    })


@app.route('/api/world/chat', methods=['POST'])
def api_world_chat_post():
    payload = request.get_json(silent=True) or {}
    from_agent = (payload.get('from') or payload.get('agent') or '').strip()
    to_agent = (payload.get('to') or '').strip()
    topic = (payload.get('topic') or 'office').strip()
    msg = (payload.get('msg') or payload.get('message') or '').strip()

    if not from_agent or not msg:
        return jsonify(success=False, message='from and msg required'), HTTPStatus.BAD_REQUEST
    if from_agent not in AGENT_DISPATCH:
        return jsonify(success=False, message=f"unknown agent '{from_agent}'"), HTTPStatus.NOT_FOUND

    msg = msg.replace("\n", " ").strip()
    if len(msg) > WORLD_CHAT_MAX_LEN:
        msg = msg[:WORLD_CHAT_MAX_LEN] + '…'

    state = _load_world_state()
    state.setdefault('chat', [])
    state.setdefault('chatCounters', {})
    state.setdefault('chatApprovals', {})

    now = datetime.now(ZoneInfo('Asia/Bangkok'))
    hour_key = now.strftime('%Y-%m-%dT%H')
    key = f"{from_agent}:{hour_key}"
    cnt = int(state['chatCounters'].get(key, 0))

    # allow if an approval is active for this from_agent/topic
    approved = False
    for token, a in (state.get('chatApprovals') or {}).items():
        if a.get('from') == from_agent and a.get('topic') == topic:
            try:
                if datetime.fromisoformat(a.get('expiresAt')) > now:
                    approved = True
                    break
            except Exception:
                pass

    if not approved and cnt >= WORLD_CHAT_LIMIT_PER_HOUR:
        return jsonify(success=False, message='chat limit reached; request permission via /api/world/chat/request'), HTTPStatus.TOO_MANY_REQUESTS

    state['chatCounters'][key] = cnt + 1

    entry = {
        'ts': now.isoformat(),
        'from': from_agent,
        'to': to_agent or None,
        'topic': topic,
        'msg': msg,
    }
    state['chat'].append(entry)
    state = _world_prune(state)
    _save_world_state(state)

    return jsonify(success=True, entry=entry)


@app.route('/api/world/chat/request', methods=['POST'])
def api_world_chat_request():
    payload = request.get_json(silent=True) or {}
    from_agent = (payload.get('from') or '').strip()
    topic = (payload.get('topic') or 'office').strip()
    reason = (payload.get('reason') or '').strip()
    minutes = int(payload.get('minutes') or 30)

    if not from_agent:
        return jsonify(success=False, message='from required'), HTTPStatus.BAD_REQUEST
    if from_agent not in AGENT_DISPATCH:
        return jsonify(success=False, message=f"unknown agent '{from_agent}'"), HTTPStatus.NOT_FOUND

    state = _load_world_state()
    state.setdefault('chatApprovals', {})

    now = datetime.now(ZoneInfo('Asia/Bangkok'))
    token = str(uuid.uuid4())
    expires = now + timedelta(minutes=minutes)
    state['chatApprovals'][token] = {
        'from': from_agent,
        'topic': topic,
        'reason': reason,
        'createdAt': now.isoformat(),
        'expiresAt': expires.isoformat(),
        'approved': None,
    }
    _save_world_state(state)

    approve_url = f"http://127.0.0.1:9000/api/world/chat/approve/{token}"
    deny_url = f"http://127.0.0.1:9000/api/world/chat/deny/{token}"

    msg = (
        f"Permission request: [{from_agent}] wants extended office chat on '{topic}' for {minutes}m.\n"
        f"Reason: {reason or '—'}\n"
        f"Approve: {approve_url}\n"
        f"Deny: {deny_url}"
    )
    _send_telegram_message(msg)

    return jsonify(success=True, token=token, expiresAt=expires.isoformat())


@app.route('/api/world/chat/approve/<token>')
def api_world_chat_approve(token: str):
    state = _load_world_state()
    a = (state.get('chatApprovals') or {}).get(token)
    if not a:
        return 'Not found', 404
    a['approved'] = True
    state['chatApprovals'][token] = a
    _save_world_state(state)
    return f"Approved for {a.get('from')} on {a.get('topic')} until {a.get('expiresAt')}"


@app.route('/api/world/chat/deny/<token>')
def api_world_chat_deny(token: str):
    state = _load_world_state()
    a = (state.get('chatApprovals') or {}).get(token)
    if not a:
        return 'Not found', 404
    a['approved'] = False
    state['chatApprovals'][token] = a
    _save_world_state(state)
    return f"Denied for {a.get('from')} on {a.get('topic')}"


@app.route('/api/world/clear/<agent>', methods=['POST'])
def api_world_clear(agent: str):
    state = _world_clear_unread(agent)
    return jsonify(success=True, state=state)


@app.route("/api/locks")
def api_locks():
    locks = _collect_lock_files()
    stale = [l for l in locks if not l.get("pidAlive")]
    agents = sorted({l["agent"] for l in locks})
    return jsonify({
        "generatedAt": datetime.utcnow().isoformat() + "Z",
        "rootHint": str(Path.home() / ".openclaw" / "agents"),
        "count": len(locks),
        "staleCount": len(stale),
        "agentCount": len(agents),
        "locks": locks,
    })


@app.route("/api/locks/<agent>/<lock_name>/release", methods=["POST"])
def api_release_lock(agent: str, lock_name: str):
    lock_path = Path.home() / ".openclaw" / "agents" / agent / "sessions" / lock_name
    if not lock_path.exists() or not lock_path.name.endswith(".lock"):
        return jsonify(success=False, message="Lock not found"), HTTPStatus.NOT_FOUND

    pid = None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = payload.get("pid")
    except Exception:
        pid = None

    if pid and isinstance(pid, int) and _pid_alive(pid):
        return jsonify(success=False, message=f"Refusing: PID {pid} still alive"), HTTPStatus.CONFLICT

    try:
        lock_path.unlink()
    except Exception as e:
        return jsonify(success=False, message=f"Failed to delete lock: {e}"), HTTPStatus.INTERNAL_SERVER_ERROR

    return jsonify(success=True, message="Lock released")

@app.route("/api/locks/release-stale", methods=["POST"])
def api_release_all_stale():
    locks = _collect_lock_files()
    stale = [l for l in locks if not l.get("pidAlive")]
    deleted = []
    errors = []
    for l in stale:
        try:
            path = Path(l.get("path", ""))
            # Safety: ensure it's within ~/.openclaw/agents/*/sessions and endswith .lock
            if not path.name.endswith('.lock'):
                continue
            home_agents = Path.home() / ".openclaw" / "agents"
            try:
                path.relative_to(home_agents)
            except Exception:
                continue
            if path.exists():
                path.unlink()
                deleted.append({"agent": l.get("agent"), "lockName": l.get("lockName")})
        except Exception as e:
            errors.append({"agent": l.get("agent"), "lockName": l.get("lockName"), "error": str(e)})

    return jsonify({
        "success": True,
        "deletedCount": len(deleted),
        "deleted": deleted,
        "errors": errors,
    })


@app.route("/api/pids/<int:pid>/terminate", methods=["POST"])
def api_terminate_pid(pid: int):
    if pid <= 1:
        return jsonify(success=False, message="Refusing to signal PID <= 1"), HTTPStatus.BAD_REQUEST

    if not _pid_alive(pid):
        return jsonify(success=False, message="PID not running"), HTTPStatus.NOT_FOUND

    # Safety: only allow terminating PIDs that currently own at least one lock file.
    locks = _collect_lock_files()
    owned = [l for l in locks if l.get("pid") == pid and l.get("pidAlive")]
    if not owned:
        return jsonify(success=False, message="Refusing: PID does not appear to own any active session locks"), HTTPStatus.CONFLICT

    # Default to TERM; allow KILL if explicitly requested.
    payload = {}
    try:
        payload = json.loads((request.data or b'{}').decode('utf-8') or '{}')
    except Exception:
        payload = {}
    sig_name = (payload.get('signal') or 'TERM').upper()
    sig = signal.SIGTERM if sig_name == 'TERM' else signal.SIGKILL if sig_name == 'KILL' else None
    if sig is None:
        return jsonify(success=False, message="Unsupported signal (use TERM or KILL)"), HTTPStatus.BAD_REQUEST

    try:
        os.kill(pid, sig)
    except PermissionError:
        return jsonify(success=False, message="Permission denied"), HTTPStatus.FORBIDDEN
    except ProcessLookupError:
        return jsonify(success=False, message="PID not running"), HTTPStatus.NOT_FOUND
    except Exception as e:
        return jsonify(success=False, message=f"Failed to signal PID: {e}"), HTTPStatus.INTERNAL_SERVER_ERROR

    return jsonify(success=True, message=f"Sent {sig_name} to PID {pid}", locks=owned)



@app.route("/health")
def health_page():
    return redirect("/mission-control-health-locks.html")

@app.route("/api/promote-deal", methods=["POST"])
def promote_deal():
    """Store a deal as an alternative for a flight (keeps main plan stable)."""
    payload = request.get_json(silent=True) or {}
    flight_id = payload.get('flight_id')
    deal = payload.get('deal') or {}
    if not flight_id:
        return jsonify({'ok': False, 'error': 'missing flight_id'}), 400

    plan_path = BASE_DIR / 'travel-flight-plan.json'
    if not plan_path.exists():
        return jsonify({'ok': False, 'error': 'plan not found'}), 404

    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    plan.setdefault('alternatives', {})
    plan['alternatives'].setdefault(flight_id, [])

    # normalize minimal deal fields
    keep = {
        'route': deal.get('route'),
        'date': deal.get('date'),
        'airline': deal.get('airline'),
        'times': deal.get('times'),
        'stops': deal.get('stops'),
        'price_per_person': deal.get('price_per_person'),
        'url': deal.get('url'),
        'saved_at': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z'),
        'savings_vs_baseline': deal.get('savings_vs_baseline'),
    }
    plan['alternatives'][flight_id].insert(0, keep)
    # cap alternatives per flight
    plan['alternatives'][flight_id] = plan['alternatives'][flight_id][:20]

    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'ok': True, 'count': len(plan['alternatives'][flight_id])})


@app.route("/calendar")
def calendar_page():
    return redirect("/mission-control-calendar.html")


@app.route("/team")
def team_page():
    return redirect("/mission-control-team.html")


@app.route("/flight")
def flight_page():
    return redirect("/mission-control-flight-plan.html")


@app.route("/team-members")
def team_members_page():
    return redirect("/mission-control-team-members.html")


@app.route("/office")
def office_page():
    return redirect("/mission-control-office.html")


@app.route("/virtual-office")
def virtual_office_page():
    return redirect("/mission-control-virtual-office.html")


@app.route("/world")
def world_page():
    return redirect("/mission-control-world.html")


@app.route("/world-topdown")
def world_topdown_page():
    return redirect("/mission-control-world-topdown.html")


@app.route("/world-hybrid")
def world_hybrid_page():
    return redirect("/mission-control-world-hybrid.html")


@app.route("/")
def dashboard():
    return redirect("/mission-control.html")


@app.route("/<path:filename>")
def serve_file(filename: str):
    if filename.endswith(".html") and filename in HTML_PAGES:
        return send_from_directory(BASE_DIR, filename)
    if filename.endswith(".json") and filename in JSON_FILES:
        return send_from_directory(BASE_DIR, filename)
    abort(404)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9000)
