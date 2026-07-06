#!/usr/bin/env python3
"""
cloud_hourly.py — runs in GitHub Actions (this PRIVATE repo) after Power Automate
pushes today's reports into hourly_reports/. Counts today's distinct ROPPs/TGLs and
publishes the SHARED hourly_state.json + hourly.json to the PUBLIC dashboard repo via
the API. Raw reports never leave this private repo. No machine needs to be on.
"""
import os, glob, json, base64
from datetime import datetime
import requests
try:
    from zoneinfo import ZoneInfo
    def _now(): return datetime.now(ZoneInfo("America/Los_Angeles"))
except Exception:
    def _now(): return datetime.utcnow()

TOKEN = os.environ["DASHBOARD_TOKEN"]
PUB = "johnschwinghamer94-lab/sierra-ropp-dashboard"
API = "https://api.github.com/repos/" + PUB + "/contents/"
H = {"Authorization": "token " + TOKEN, "Accept": "application/vnd.github+json"}


def _rows(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    return [list(r) for r in wb.active.iter_rows(values_only=True)]


def _isjob(v):
    if isinstance(v, (int, float)): return v > 0
    if isinstance(v, str): return v.strip().isdigit()
    return False


def _jk(v):
    return str(int(v)) if isinstance(v, (int, float)) else str(v).strip()


def _find(*subs):
    for p in sorted(glob.glob("hourly_reports/*.xlsx"), key=os.path.getmtime, reverse=True):
        n = os.path.basename(p).lower()
        if all(s in n for s in subs):
            return p
    return None


def count_today():
    calls, tgls = set(), set()
    rev = _find("revenue")
    if rev:
        for r in _rows(rev):
            bu = r[10] if len(r) > 10 and isinstance(r[10], str) else ""
            if "HVAC" in bu and ("Service" in bu or "Maintenance" in bu) and len(r) > 3 and _isjob(r[3]):
                calls.add(_jk(r[3]))
    tgl = _find("tgls", "created") or _find("tgls")
    if tgl:
        for r in _rows(tgl):
            if len(r) > 1 and _isjob(r[1]):
                tgls.add(_jk(r[1]))
    return len(calls), len(tgls)


def rate(a, b):
    return round(a / b * 1000) / 10 if b else 0.0


def get(path):
    r = requests.get(API + path, headers=H)
    if r.status_code == 200:
        j = r.json()
        return json.loads(base64.b64decode(j["content"])), j["sha"]
    return None, None


def put(path, obj, sha, msg):
    body = {"message": msg, "content": base64.b64encode(json.dumps(obj).encode()).decode(), "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(API + path, headers=H, json=body)
    r.raise_for_status()


def main():
    n = _now(); today = n.date().isoformat(); hh = f"{n.hour:02d}"
    calls, tgls = count_today()

    st, _ = get("hourly_state.json")               # shared state from public repo
    if not st or st.get("date") != today:
        st = {"date": today, "hours": {}}
    st["hours"][hh] = {"calls": calls, "tgls": tgls}

    hrs = sorted(st["hours"]); series = []; pc = pt = 0
    for h in hrs:
        c = st["hours"][h]["calls"]; t = st["hours"][h]["tgls"]
        series.append({"hour": h, "calls": c, "tgls": t, "rate": rate(t, c),
                       "dcalls": max(c-pc, 0), "dtgls": max(t-pt, 0), "drate": rate(max(t-pt, 0), max(c-pc, 0))})
        pc, pt = c, t
    latest = series[-1] if series else {"calls": 0, "tgls": 0, "rate": 0}
    out = {"date": today, "updated": n.strftime("%-I:%M %p"),
           "today": {"calls": latest["calls"], "tgls": latest["tgls"], "rate": latest["rate"]},
           "hours": series}

    _, ssha = get("hourly_state.json")
    put("hourly_state.json", st, ssha, f"Cloud hourly state {today} {hh}:00")
    _, osha = get("hourly.json")
    put("hourly.json", out, osha, f"Cloud hourly capture {today} {hh}:00")
    print(f"Cloud captured {today} {hh}:00 -> {calls} calls / {tgls} TGLs ({rate(tgls, calls)}%)")


if __name__ == "__main__":
    main()
