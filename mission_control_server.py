"""Mission Control helper server."""
import json
import os
import shlex
import signal
import subprocess
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Dict, List

from flask import Flask, abort, jsonify, redirect, send_from_directory, request

BASE_DIR = Path(__file__).parent.resolve()
app = Flask(__name__, static_folder=None)

COMMANDS: Dict[str, str] = {
    "travel": "openclaw agent --local --agent super-jobs --message \"Use Topic Monitor to watch Bangkok to Japan fares and surface the cheapest legs.\"",
    "manifesto": "openclaw agent --local --agent super-jobs --message \"Use morning-manifesto to plan tomorrow's top 3 travel actions.\"",
    "memory": "openclaw agent --local --agent super-jobs --message \"Use supermemory to store: [paste your note here]\"",
    "token": "openclaw cron list",
    "watch": "openclaw agent --local --agent super-jobs --message \"Refresh travel alert for Bangkok to Japan routes.\"",
    "pricewatch": "openclaw cron run d39d96cc-b37b-4cd6-9ff9-2c0bc2b39c9e --expect-final --timeout 600000",
    "deals": "openclaw cron run d39d96cc-b37b-4cd6-9ff9-2c0bc2b39c9e --expect-final --timeout 600000"
}

statuses = {
    "travel": {"label": "Travel Scan", "state": "idle", "detail": "Last run: none"},
    "manifesto": {"label": "Morning Manifesto", "state": "idle", "detail": "Last run: none"},
    "memory": {"label": "Memory Snapshot", "state": "idle", "detail": "Last run: none"},
    "token": {"label": "Token Pulse", "state": "idle", "detail": "Last run: none"},
    "watch": {"label": "Travel Watch", "state": "idle", "detail": "Last run: none"},
    "pricewatch": {"label": "Price Check Now", "state": "idle", "detail": "Last run: none"},
    "deals": {"label": "Deals Radar", "state": "idle", "detail": "Last run: none"},
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
            ["openclaw", "sessions", "--json"],
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
    return usage[:5]


def set_status(action: str, state: str, detail: str) -> None:
    if action in statuses:
        statuses[action]["state"] = state
        statuses[action]["detail"] = detail


@app.route("/run/<action>")
def run_action(action: str):
    command = COMMANDS.get(action)
    if not command:
        return jsonify(success=False, message="Unknown action"), HTTPStatus.NOT_FOUND
    set_status(action, "running", "Working…")
    proc = subprocess.run(shlex.split(command), cwd=str(BASE_DIR), capture_output=True, text=True)
    output = proc.stdout.strip() or proc.stderr.strip() or "Done."
    detail = output.replace("\n", " ")[:120]
    set_status(action, "idle", f"Last run: {detail}")    # If we just ran a price check, stamp last-checked + compute deltas + append history.
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
