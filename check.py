#!/usr/bin/env python3
"""
KoulchiConnect status checker. Run by GitHub Actions every 5 minutes.

Each check talks to the live server the way the app does, records whether it
worked and how long it took, and folds the result into data/history.json
(one entry per component per day, 90 days kept) and data/latest.json.

Settings come from GitHub Actions secrets (never stored in this repo):
  SUPABASE_URL, SUPABASE_ANON_KEY   the app's public server address and key
  KC_CLIENT_EMAIL, KC_CLIENT_PASSWORD   the TEST client account (login check)

No user data is read or stored: only up/down and response times.
"""
import asyncio, json, os, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_ANON_KEY"]
DATA = Path(os.environ.get("STATUS_DATA", Path(__file__).parent / "data"))
SLOW_MS = 2500
KEEP_DAYS = 90

COMPONENTS = [
    ("api", "App server (API)"),
    ("login", "Login"),
    ("database", "Database"),
    ("realtime", "Chat & live updates"),
    ("storage", "Photos & documents"),
    ("push", "Push notifications"),
    ("supabase", "Supabase platform (Stockholm)"),
]
SUPABASE_REGION = "eu-north-1"
RECENT = 288          # last 24 h of checks kept for the speed chart


def http(method, path, body=None, token=None, timeout=15):
    h = {"apikey": KEY, "Authorization": "Bearer " + (token or KEY), "Content-Type": "application/json"}
    req = urllib.request.Request(URL + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(); code = r.status
    except urllib.error.HTTPError as e:
        data = e.read(); code = e.code
    except Exception as e:
        return 0, None, (time.perf_counter() - t0) * 1000, str(e)[:120]
    return code, data, (time.perf_counter() - t0) * 1000, None


def check_api():
    code, _, ms, err = http("GET", "/rest/v1/")
    return code in (200, 401, 404) and err is None, ms, err or f"HTTP {code}"


def check_login():
    code, data, ms, err = http("POST", "/auth/v1/token?grant_type=password",
                               {"email": os.environ["KC_CLIENT_EMAIL"],
                                "password": os.environ["KC_CLIENT_PASSWORD"]})
    ok = code == 200 and data and b"access_token" in data
    return bool(ok), ms, err or f"HTTP {code}"


def check_database():
    code, data, ms, err = http("GET", "/rest/v1/service_catalog_categories?select=id&limit=1")
    ok = code == 200 and data is not None and data.strip().startswith(b"[")
    return bool(ok), ms, err or f"HTTP {code}"


def check_storage():
    # A missing file must answer quickly with 400/404: the service is up.
    code, _, ms, err = http("GET", "/storage/v1/object/public/profiles/status-check-missing.png")
    return code in (400, 404) and err is None, ms, err or f"HTTP {code}"


def check_push():
    # A stranger must be refused: proves the function is deployed and guarded.
    code, _, ms, err = http("POST", "/functions/v1/send-push-notification",
                            {"userId": "00000000-0000-0000-0000-000000000000", "title": "t", "body": "b"})
    return code == 401, ms, err or f"HTTP {code}"


def check_realtime():
    import websockets  # installed by the workflow

    async def run():
        ws_url = URL.replace("https://", "wss://") + f"/realtime/v1/websocket?apikey={KEY}&vsn=1.0.0"
        t0 = time.perf_counter()
        async with websockets.connect(ws_url, open_timeout=10) as ws:
            await ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": "1"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            ms = (time.perf_counter() - t0) * 1000
            return reply.get("payload", {}).get("status") == "ok", ms, None
    try:
        return asyncio.run(run())
    except Exception as e:
        return False, 0, str(e)[:120]


def check_supabase():
    # Supabase's own public status for our region: tells their outages
    # apart from ours.
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen("https://status.supabase.com/api/v2/components.json", timeout=15) as r:
            comps = json.loads(r.read())["components"]
    except Exception as e:
        return False, 0, "status feed unreachable: " + str(e)[:80], "down"
    ms = (time.perf_counter() - t0) * 1000
    states = {c["status"] for c in comps if c["name"] == SUPABASE_REGION}
    if "major_outage" in states:
        return False, ms, "Supabase reports a major outage in " + SUPABASE_REGION, "down"
    if states & {"partial_outage", "degraded_performance", "under_maintenance"}:
        return True, ms, "Supabase reports " + ", ".join(sorted(states - {"operational"})).replace("_", " "), "slow"
    return True, ms, None, "up"


CHECKS = {"api": check_api, "login": check_login, "database": check_database,
          "realtime": check_realtime, "storage": check_storage, "push": check_push,
          "supabase": check_supabase}


def main():
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    recent_file = DATA / "recent.json"
    recent = json.loads(recent_file.read_text()) if recent_file.exists() else {}
    hist_file = DATA / "history.json"
    hist = json.loads(hist_file.read_text()) if hist_file.exists() else {}
    latest = {"checked_at": now.isoformat(timespec="seconds"), "components": []}

    for key, name in COMPONENTS:
        forced = None
        try:
            res = CHECKS[key]()
            ok, ms, why = res[:3]
            forced = res[3] if len(res) > 3 else None
        except Exception as e:
            ok, ms, why = False, 0, str(e)[:120]
        state = forced or ("down" if not ok else ("slow" if ms > SLOW_MS else "up"))
        days = hist.setdefault(key, {})
        d = days.setdefault(day, {"checks": 0, "up": 0, "slow": 0, "ms_total": 0, "incidents": []})
        d["checks"] += 1
        d["up"] += ok
        d["slow"] += state == "slow"
        d["ms_total"] += round(ms)
        # Open an incident on a failure, close it on the next success.
        open_inc = d["incidents"][-1] if d["incidents"] and d["incidents"][-1].get("end") is None else None
        prev_open = None
        if not open_inc:
            for dd in sorted(days)[::-1][1:2]:
                incs = days[dd]["incidents"]
                if incs and incs[-1].get("end") is None:
                    prev_open = incs[-1]
        if not ok and not open_inc and not prev_open:
            d["incidents"].append({"start": now.strftime("%H:%M"), "end": None,
                                   "reason": why if why and not why.startswith("HTTP 2") else "no answer"})
        elif ok and (open_inc or prev_open):
            (open_inc or prev_open)["end"] = now.strftime("%H:%M")
        for old in [k for k in days if k < _days_ago(now, KEEP_DAYS)]:
            del days[old]
        if key != "supabase":
            pts = recent.setdefault(key, [])
            pts.append([now.strftime("%Y-%m-%dT%H:%M"), round(ms), state[0]])
            del pts[:-RECENT]
        latest["components"].append({"key": key, "name": name, "state": state, "ms": round(ms)})
        print(f"{name:24} {state:5} {round(ms):6} ms  {'' if ok else why}")

    recent_file.write_text(json.dumps(recent, separators=(",", ":")))
    hist_file.write_text(json.dumps(hist, separators=(",", ":")))
    (DATA / "latest.json").write_text(json.dumps(latest, indent=1))
    (DATA / "components.json").write_text(json.dumps([{"key": k, "name": n} for k, n in COMPONENTS]))
    down = [c["name"] for c in latest["components"] if c["state"] == "down"]
    Path(os.environ.get("GITHUB_OUTPUT", "/dev/null")).open("a").write(f"down={', '.join(down)}\n")


def _days_ago(now, n):
    from datetime import timedelta
    return (now - timedelta(days=n)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
