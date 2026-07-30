#!/usr/bin/env python3
"""HVAC-Service Live Feed sync — per-job lifecycle tracker for the Service dept
live feed (clone of livefeed_sync.py's SILO engine, retargeted at HVAC Service).

Tracks every job today that has a roster Service tech on it (BU 333 techs,
teams "2a Service NO Sam Maintenance" / "2b Service with SAM Maintenance"),
through:
    DISPATCHED -> ON SITE -> OPTIONS ($) -> SIGNED ($) -> DONE
Service techs run both HVAC - Service (333) and HVAC - Maintenance (342817560)
jobs; the job's own BU is carried for display, not filtered on.

Entity APIs only (never the rate-limited reporting API):
    settings/v2/technicians (roster), jpm/appointments + jobs,
    dispatch/appointment-assignments, crm/customers, sales/estimates
    (options = active unsold; signed = soldOn today), forms/v2 job attachments
    (photo count probe).

Runs in TWO environments (same file, keep private repo & servicetitan/ in sync):
  Windows (default, no env): publishes servicefeed.json LOCALLY only
    (servicetitan/ folder) — no git, no cloud push. NEVER posts to any
    bonus-sheet webhook (livefeed_sync already logs every dept TGL/lead;
    this file must not duplicate that).
  Cloud (SERVICEFEED_CLOUD=1, GitHub Actions): publishes servicefeed.json +
    servicefeed_state.json to the dashboard repo via the GitHub contents API
    with DASHBOARD_TOKEN (PAT commits trigger the Pages deploy; GITHUB_TOKEN
    commits would NOT). State is seeded from the repo at session start so
    5-hour relay sessions hand off cleanly. Creds come from ST_CREDS_JSON
    (materializes ~/.servicetitan/sierra.json). Ported from livefeed_sync.py's
    proven cloud machinery.

Usage:
    python servicefeed_sync.py            # loop: one cycle / 90s, 06:50-midnight
    python servicefeed_sync.py --once     # single cycle now (ignores time window)
    python servicefeed_sync.py --dry      # single cycle, print JSON, no write
"""
import base64, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import st_client as st

CLOUD = os.environ.get("SERVICEFEED_CLOUD") == "1"
SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "servicefeed.json"
STATE = SCRIPT_DIR / "servicefeed_state.json"
LOG = SCRIPT_DIR / "servicefeed_log.txt"

# cloud publish target + session cap (GitHub Actions jobs die at 6 h)
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
MAX_MIN = int(os.environ.get("SERVICEFEED_MAX_MIN", "310"))

CYCLE_SECS = 60   # near-live board (John 7/30)
DAY_START = (6, 50)
DAY_END = (23, 59)

SERVICE_BU_ID = 333
SERVICE_TEAMS = {"2a Service NO Sam Maintenance": "2A",
                  "2b Service with SAM Maintenance": "2B"}

# membership / SAM item pattern, found by sampling real sold estimates
# (sku.name starts with SAM / PLSAM, or displayName mentions Membership /
# Maintenance Agreement / Service Agreement)
def _is_membership_item(sku):
    nm = (sku.get("name") or "").upper()
    disp = (sku.get("displayName") or "").lower()
    if nm.startswith("SAM") or nm.startswith("PLSAM"):
        return True
    return any(x in disp for x in ("membership", "maintenance agreement", "service agreement"))


# ── Siro call-recording status (John, 2026-07-30) ───────────────────────────
# Minimal port of livefeed_sync.py's Siro client (same mint/auth pattern,
# same team id, same UA). Cached module-lifetime token; recordings list is
# cached and refreshed at most every 5 minutes (the relay cycles every ~90s,
# don't hammer Siro every cycle). FAILS OPEN on any error (creds absent,
# network, 4xx): returns None, and the caller sets siro:null on every job /
# siroToday:null on the payload rather than ever crashing the relay.
SIRO_TEAM_ID = "Q42L8L"
SIRO_TOKEN_URL_FMT = "https://functions.siro.ai/api-externalApi/v1/core/oauth/apps/{client_id}/access-token"
SIRO_API_BASE = "https://api.siro.ai/v1/core"
SIRO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SIRO_LOCAL_API_KEY = Path.home() / ".siro_api_key"
_SIRO_LOCAL_OAUTH_APP = Path.home() / ".siro_oauth_app.json"
_SIRO_LOCAL_TEST_USER_ID = "RKnwDLk8seVkMrLIhDdnSOVsEu73"
_SIRO_TOKEN = {"tok": None}
_SIRO_CACHE = {"ts": 0.0, "data": None}   # data: {norm_full_name: {"n": int, "starts": [datetime]}}
SIRO_REFRESH_SECS = 90   # near-live: REC-now badge tracks in-progress recordings


def _siro_creds():
    api_key = os.environ.get("SIRO_API_KEY")
    client_id = os.environ.get("SIRO_CLIENT_ID")
    client_secret = os.environ.get("SIRO_CLIENT_SECRET")
    user_id = os.environ.get("SIRO_USER_ID")
    if not api_key and _SIRO_LOCAL_API_KEY.exists():
        try:
            api_key = _SIRO_LOCAL_API_KEY.read_text().strip()
        except OSError:
            pass
    if (not client_id or not client_secret) and _SIRO_LOCAL_OAUTH_APP.exists():
        try:
            app = json.loads(_SIRO_LOCAL_OAUTH_APP.read_text())
            client_id = client_id or app.get("clientID")
            client_secret = client_secret or app.get("clientSecret")
        except (OSError, json.JSONDecodeError):
            pass
    if not user_id and _SIRO_LOCAL_API_KEY.exists() and _SIRO_LOCAL_OAUTH_APP.exists():
        user_id = _SIRO_LOCAL_TEST_USER_ID
    return api_key, client_id, client_secret, user_id


def _siro_mint_token(api_key, client_id, client_secret, user_id):
    url = SIRO_TOKEN_URL_FMT.format(client_id=client_id)
    r = requests.post(url, json={"clientSecret": client_secret, "userId": user_id, "scope": "read"},
                       headers={"Authorization": f"Bearer {api_key}", "User-Agent": SIRO_UA}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]


def _siro_fetch_recordings(token):
    r = requests.get(f"{SIRO_API_BASE}/recordings?teamId={SIRO_TEAM_ID}&limit=50",
                      headers={"x-siro-auth-token": token, "User-Agent": SIRO_UA}, timeout=30)
    if r.status_code == 401:
        raise PermissionError("401")
    r.raise_for_status()
    return r.json().get("data", [])


_siro_norm = lambda s: " ".join((s or "").lower().split())


def _siro_rec_dt(rec):
    """Best-effort creation/start timestamp for a Siro recording object —
    field name isn't pinned down in our docs, so try the plausible ones."""
    for k in ("createdAt", "createdOn", "startedAt", "startTime", "created", "start"):
        v = rec.get(k)
        if v:
            dt = parse_utc(v) if isinstance(v, str) and ("T" in v or "Z" in v) else None
            if dt:
                return dt
            try:
                # epoch seconds/millis fallback
                num = float(v)
                if num > 1e12:
                    num /= 1000.0
                return datetime.fromtimestamp(num, tz=timezone.utc).astimezone()
            except Exception:
                continue
    return None


def fetch_siro_today():
    """Today's (local/PT) Siro recordings, grouped by normalized rep full
    name: {"n": count, "starts": [aware local datetimes]}. Cached at most
    SIRO_REFRESH_SECS; returns None (and logs once) on any failure or when
    creds are unavailable — caller must degrade silently."""
    now_ts = time.time()
    if _SIRO_CACHE["data"] is not None and now_ts - _SIRO_CACHE["ts"] < SIRO_REFRESH_SECS:
        return _SIRO_CACHE["data"]
    api_key, client_id, client_secret, user_id = _siro_creds()
    if not all([api_key, client_id, client_secret, user_id]):
        log("siro: creds absent — siro:null this cycle")
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = None
        return None
    try:
        tok = _SIRO_TOKEN["tok"]
        if not tok:
            tok = _siro_mint_token(api_key, client_id, client_secret, user_id)
            _SIRO_TOKEN["tok"] = tok
        try:
            recs = _siro_fetch_recordings(tok)
        except PermissionError:
            tok = _siro_mint_token(api_key, client_id, client_secret, user_id)
            _SIRO_TOKEN["tok"] = tok
            recs = _siro_fetch_recordings(tok)
        today_local = datetime.now().astimezone().date()
        out = {}
        for rec in recs:
            live = (rec.get("result") or "").strip().lower() == "in progress"
            dt = _siro_rec_dt(rec)
            # in-progress = happening now, keep it even if the timestamp field is absent
            if not live and (not dt or dt.date() != today_local):
                continue
            fn = _siro_norm(rec.get("repFirstName"))
            ln = _siro_norm(rec.get("repLastName"))
            full = _siro_norm(f"{fn} {ln}").strip()
            if not full:
                continue
            ent = out.setdefault(full, {"n": 0, "starts": [], "live": False})
            ent["n"] += 1
            if dt:
                ent["starts"].append(dt)
            if live:
                ent["live"] = True
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = out
        return out
    except Exception as ex:
        log("siro FAILED (fail-open, siro:null): " + repr(ex)[:150])
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = None
        return None


def _siro_match(tech_name, siro_data):
    """Tolerant match of a roster tech name to a Siro rep entry — exact
    normalized full-name match first, else first-name match against any
    Siro entry's first token. None if no Siro data / no match."""
    if siro_data is None:
        return None
    norm = _siro_norm(tech_name)
    if not norm:
        return None
    if norm in siro_data:
        return siro_data[norm]
    first = norm.split()[0] if norm.split() else ""
    if first:
        for k, v in siro_data.items():
            kparts = k.split()
            if kparts and kparts[0] == first:
                return v
    return None


def log(msg):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line)
    try:
        if LOG.exists() and LOG.stat().st_size > 400_000:
            LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-1500:]) + "\n", encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fmt_t(dt_local):
    s = dt_local.strftime("%I:%M%p").lstrip("0")
    return s[:-2] + ("p" if s.endswith("PM") else "a")


def parse_utc(s):
    if not s:
        return None
    try:
        base = s.replace("Z", "").split(".")[0].split("+")[0]
        return datetime.fromisoformat(base).replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return None


def paged(path, params, tenant="SIE"):
    out, page = [], 1
    while True:
        r = st.api_get(path, dict(params, page=page, pageSize=500), tenant)
        out += r.get("data", [])
        if not r.get("hasMore"):
            return out
        page += 1
        time.sleep(0.15)


def chunked_get(path, ids, extra=None):
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 50):
        params = dict(extra or {})
        params["ids"] = ",".join(str(x) for x in ids[i:i + 50])
        out += paged(path, params)
        time.sleep(0.15)
    return out


# ── roster (dynamic, cached once/day) ───────────────────────────────────────

def fetch_roster():
    """{tech_name: '2A'|'2B'} for active BU-333 techs on the two target teams."""
    techs = paged("/settings/v2/tenant/{tenant}/technicians", {})
    roster = {}
    for t in techs:
        if not t.get("active"):
            continue
        if t.get("businessUnitId") != SERVICE_BU_ID:
            continue
        team = SERVICE_TEAMS.get(t.get("team"))
        if team:
            roster[t.get("name", "").strip()] = team
    return roster


def get_roster(state):
    today = date.today().isoformat()
    cached = state.get("roster")
    if cached and cached.get("date") == today and cached.get("map"):
        return cached["map"]
    try:
        rmap = fetch_roster()
    except Exception as ex:
        log("roster fetch FAILED, using stale cache if any: " + repr(ex)[:150])
        return (cached or {}).get("map", {})
    state["roster"] = {"date": today, "map": rmap}
    return rmap


# ── lookups (BU / job-type names), cached once/day ─────────────────────────

_LOOKUPS = {}
def _lookups(today):
    if _LOOKUPS.get("date") != today.isoformat():
        _LOOKUPS["bus"] = {b["id"]: b.get("name", "") for b in paged("/settings/v2/tenant/{tenant}/business-units", {})}
        _LOOKUPS["jts"] = {t["id"]: t.get("name", "") for t in paged("/jpm/v2/tenant/{tenant}/job-types", {})}
        _LOOKUPS["date"] = today.isoformat()
    return _LOOKUPS["bus"], _LOOKUPS["jts"]


# ── photo count probe ───────────────────────────────────────────────────────

_PHOTO_ENDPOINT_OK = None  # None = untested, True/False once known this session
def photo_count(job_id, tenant="SIE"):
    global _PHOTO_ENDPOINT_OK
    if _PHOTO_ENDPOINT_OK is False:
        return None
    try:
        r = st.api_get("/forms/v2/tenant/{tenant}/jobs/" + str(job_id) + "/attachments",
                       {"page": 1, "pageSize": 1}, tenant)
        _PHOTO_ENDPOINT_OK = True
        tc = r.get("totalCount")
        if tc is not None:
            return tc
        # totalCount not populated by this API — page through cheaply
        n, page = 0, 1
        while True:
            rr = st.api_get("/forms/v2/tenant/{tenant}/jobs/" + str(job_id) + "/attachments",
                            {"page": page, "pageSize": 50}, tenant)
            n += len(rr.get("data", []))
            if not rr.get("hasMore"):
                break
            page += 1
        return n
    except Exception:
        _PHOTO_ENDPOINT_OK = False
        return None


# ── one polling cycle: fetch ────────────────────────────────────────────────

def fetch_today(roster):
    today = date.today()
    day0 = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc)
    day1 = day0 + timedelta(days=1)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

    appts = paged("/jpm/v2/tenant/{tenant}/appointments",
                  {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
    appts = [a for a in appts if a.get("status") not in ("Canceled",)]
    if not appts:
        return today, [], {}, {}, {}, {}, []

    # appointment-assignments date filters don't work — must go via
    # appointment IDs (same limitation livefeed_sync works around)
    asg = []
    aids = [a["id"] for a in appts]
    for i in range(0, len(aids), 50):
        asg += paged("/dispatch/v2/tenant/{tenant}/appointment-assignments",
                     {"appointmentIds": ",".join(str(x) for x in aids[i:i + 50])})
        time.sleep(0.15)
    tech_by_appt = {}
    for a in asg:
        if a.get("active") and a.get("technicianName") in roster:
            tech_by_appt.setdefault(a["appointmentId"], []).append(a["technicianName"])

    svc_appts = [a for a in appts if a["id"] in tech_by_appt]
    job_ids = sorted({a["jobId"] for a in svc_appts})
    if not job_ids:
        return today, [], {}, {}, {}, {}, []

    jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)}
    cust_ids = sorted({j.get("customerId") for j in jobs.values() if j.get("customerId")})
    custs = {c["id"]: c.get("name", "") for c in chunked_get("/crm/v2/tenant/{tenant}/customers", cust_ids)}

    bus, jts = _lookups(today)
    for j in jobs.values():
        j["_bu"] = bus.get(j.get("businessUnitId"), "")
        j["_jt"] = jts.get(j.get("jobTypeId"), "")

    # estimates touched today (options) + sold today (money on the job)
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"modifiedOnOrAfter": iso(day0)})
    time.sleep(0.15)
    ests += [e for e in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": iso(day0)})
             if e["id"] not in {x["id"] for x in ests}]
    est_by_job = {}
    for e in ests:
        if e.get("jobId") in jobs:
            est_by_job.setdefault(e["jobId"], []).append(e)

    # LEADS SET — service equivalent of TGL created: an Estimate-type lead job
    # created today whose jobGeneratedLeadSource points at a tracked job, and
    # whose job-type name startswith "Estimate" (not Install-type). Identical
    # detection logic to livefeed_sync's TGL rule; NO sheet/webhook posting —
    # livefeed_sync already logs every dept lead, this must not duplicate it.
    lead_by_src = {}
    for lj in paged("/jpm/v2/tenant/{tenant}/jobs", {"createdOnOrAfter": iso(day0)}):
        gls = lj.get("jobGeneratedLeadSource") or {}
        src = gls.get("jobId")
        if not src or src not in jobs:
            continue
        jtn = jts.get(lj.get("jobTypeId")) or ""
        if not jtn.startswith("Estimate"):
            continue
        dtl = parse_utc(lj.get("createdOn"))
        ent = lead_by_src.setdefault(src, {"n": 0, "t": None})
        ent["n"] += 1
        if ent["t"] is None and dtl:
            ent["t"] = fmt_t(dtl)

    return today, svc_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src


# ── build payload ────────────────────────────────────────────────────────────

def build(state):
    roster = get_roster(state)
    today, svc_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src = fetch_today(roster)
    now = datetime.now().astimezone()
    now_s = fmt_t(now)
    dkey = today.isoformat()

    first_run_of_day = state.get("date") != dkey
    if first_run_of_day:
        keep_roster = state.get("roster")
        state.clear()
        state.update({"date": dkey, "jobs": {}, "feed": [], "hourly": {}})
        if keep_roster:
            state["roster"] = keep_roster
    seen = state["jobs"]
    feed = state["feed"]

    def event(icon, text, color):
        if not first_run_of_day:
            feed.insert(0, {"i": icon, "x": text, "t": now_s, "c": color})

    by_job = {}
    for a in sorted(svc_appts, key=lambda x: x.get("start") or ""):
        by_job.setdefault(a["jobId"], a)

    RANK = {"Working": 0, "Dispatched": 1, "Hold": 2, "Scheduled": 3, "Done": 4}
    siro_data = fetch_siro_today()   # one Siro check per cycle (fail-open -> None)
    siro_techs_seen = set()
    cards = []
    on_site = 0
    opt_total = opt_count = 0
    opt_jobs = set()
    signed_total = 0.0
    signed_jobs = set()
    leads_set_n = 0
    memberships_sold = 0

    for jid, appt in by_job.items():
        j = jobs.get(jid, {})
        techs = sorted(set(sum((tech_by_appt.get(a["id"], []) for a in svc_appts if a["jobId"] == jid), [])))
        team_set = sorted({roster.get(t, "") for t in techs} - {""})
        cust = custs.get(j.get("customerId"), "") or "—"
        status = appt.get("status") or "Scheduled"
        if j.get("jobStatus") == "Completed" or status == "Done":
            status = "Done"
        js = seen.setdefault(str(jid), {"st": {}, "opt": 0.0, "sold": 0.0, "status": ""})

        for stage, hit in (("dispatched", status in ("Dispatched", "Working", "Done")),
                           ("onsite", status in ("Working", "Done")),
                           ("done", status == "Done")):
            if hit and stage not in js["st"]:
                js["st"][stage] = "✓" if first_run_of_day else now_s
                if stage == "dispatched":
                    event("\U0001F69A", "Dispatched: " + ", ".join(techs) + " @ " + cust, "#9fb3cc")
                elif stage == "onsite":
                    event("\U0001F4CD", "On site: " + ", ".join(techs) + " @ " + cust, "#c084fc")
                elif stage == "done":
                    event("\U0001F3C1", "Completed: " + ", ".join(techs) + " @ " + cust, "#7fb3e8")

        opts_t = opts_n = 0
        opt_time = sold_time = None
        sold_t = 0.0
        sold_flag = False
        membership_sold_job = False
        membership_offered_job = False
        for est in est_by_job.get(jid, []):
            sub = float(est.get("subtotal") or 0)
            st_name = ((est.get("status") or {}).get("name") or "").lower()
            sold = bool(est.get("soldOn")) or st_name == "sold"
            items = est.get("items") or []
            skus = [(i.get("sku") or {}) for i in items]
            is_ca_placeholder = {(s.get("name") or "") for s in skus} == {"CA01"}
            has_membership = any(_is_membership_item(s) for s in skus)
            if has_membership:
                membership_offered_job = True
                if sold:
                    membership_sold_job = True
            if sold:
                sold_t += sub
                sold_flag = True
                dtl = parse_utc(est.get("soldOn"))
                if dtl and dtl.date() == today:
                    sold_time = fmt_t(dtl)
            elif est.get("active") and st_name != "dismissed" and sub > 0:
                opts_t += sub
            if est.get("active") and st_name != "dismissed" and not is_ca_placeholder:
                opts_n += 1
                dtl = parse_utc(est.get("createdOn"))
                if dtl:
                    opt_time = fmt_t(dtl)

        lead = lead_by_src.get(jid)
        lead_n = lead["n"] if lead else 0
        lead_time = lead["t"] if lead else None

        if opts_t > js.get("opt", 0) + 0.5:
            event("\U0001F6E0", "Building options: " + ", ".join(techs) + " @ " + cust +
                  " [$" + format(int(opts_t), ",") + "]", "#F5B324")
        if sold_t > js.get("sold", 0) + 0.5:
            event("\U0001F4B5", "Sold: " + ", ".join(techs) + " @ " + cust +
                  " [+$" + format(int(sold_t - js.get("sold", 0)), ",") + "]", "#7fb3e8")
        prev_lead = js.get("leadN")
        if prev_lead is None:
            js["leadN"] = lead_n
        elif lead_n > prev_lead:
            event("✅", "LEAD SET: " + ", ".join(techs) + " @ " + cust, "#4ADE80")
        js["opt"], js["sold"], js["leadN"], js["status"] = opts_t, sold_t, lead_n, status

        if status == "Working":
            on_site += 1
        if opts_n:
            opt_total += opts_t; opt_count += opts_n; opt_jobs.add(jid)
        if sold_flag:
            signed_jobs.add(jid)
        signed_total += sold_t
        if lead_n:
            leads_set_n += lead_n
        if membership_sold_job:
            memberships_sold += 1

        start_l = parse_utc(appt.get("start"))
        done_l = parse_utc(j.get("completedOn"))

        photos = photo_count(jid)

        # Siro recording status (John, 2026-07-30): "n" = tech's recordings
        # today, "rec" = any recording started within the job's onsite window
        # (appt start - 15min .. now/done). null when Siro data unavailable.
        siro = None
        if techs:
            siro_techs_seen.update(techs)
        if siro_data is not None and techs:
            n_total = 0
            rec_flag = False
            live_flag = False
            matched_any = False
            win_start = (start_l - timedelta(minutes=15)) if start_l else None
            win_end = done_l if (status == "Done" and done_l) else now
            for t in techs:
                ent = _siro_match(t, siro_data)
                if not ent:
                    continue
                matched_any = True
                n_total += ent["n"]
                if ent.get("live"):
                    live_flag = True
                if win_start:
                    for st_dt in ent["starts"]:
                        if win_start <= st_dt <= win_end:
                            rec_flag = True
            if matched_any:
                siro = {"n": n_total, "rec": rec_flag, "recNow": live_flag}
            else:
                siro = {"n": 0, "rec": False, "recNow": False}

        cards.append({
            "jobId": jid, "jobNumber": j.get("jobNumber", ""),
            "tech": ", ".join(techs), "team": ", ".join(team_set),
            "customer": cust, "bu": j.get("_bu", ""), "jobType": j.get("_jt", ""),
            "status": status, "rank": RANK.get(status, 3),
            "start": fmt_t(start_l) if start_l else "",
            "startIso": appt.get("start") or "9999",
            "stages": {
                "dispatched": js["st"].get("dispatched"),
                "onsite": js["st"].get("onsite"),
                "options": {"t": opt_time, "n": opts_n, "total": int(opts_t)} if opts_n else None,
                "signed": {"t": sold_time, "total": int(sold_t)} if sold_flag else None,
                "leadSet": {"t": lead_time, "n": lead_n} if lead_n else None,
                "done": (fmt_t(done_l) if done_l else None) or js["st"].get("done"),
            },
            "membershipOffered": membership_offered_job,
            "membershipSold": membership_sold_job,
            "photos": photos,
            "siro": siro,
        })

    cards.sort(key=lambda c: c["startIso"])

    if first_run_of_day:
        feed.insert(0, {"i": "\U0001F4E1", "x": "Service Feed online — tracking " + str(len(cards)) +
                        " job" + ("" if len(cards) == 1 else "s") + " today", "t": now_s, "c": "#2E78C7"})
    del feed[60:]
    state["date"] = dkey

    # hour-by-hour cumulative KPI snapshots for the day: each entry is the
    # state as of the end of that hour; current hour overwrites live until it
    # closes out. Feeds the dashboard HXH tab.
    done_ct = sum(1 for c in cards if c["stages"].get("done"))
    hrs = state.setdefault("hourly", {})
    hrs[str(now.hour)] = {
        "h": now.hour, "jobs": len(cards), "done": done_ct,
        "opts": opt_count, "optsTotal": int(opt_total),
        "signed": len(signed_jobs), "signedTotal": int(signed_total),
        "leads": leads_set_n, "mems": memberships_sold,
    }

    siro_today = None
    if siro_data is not None:
        recorded = sum(1 for t in siro_techs_seen
                       if (_siro_match(t, siro_data) or {}).get("n", 0) >= 1)
        siro_today = {"recorded": recorded, "of": len(siro_techs_seen)}

    payload = {
        "date": dkey,
        "day": now.strftime("%A, %B %d").upper(),
        "generated": now_s,
        "generatedMs": int(time.time() * 1000),
        "siroToday": siro_today,
        "kpis": {
            "jobsToday": len(cards),
            "onSiteNow": on_site,
            "optionsInPlayCount": opt_count,
            "optionsInPlayTotal": int(opt_total),
            "signedJobs": len(signed_jobs),
            "signedTotal": int(signed_total),
            "leadsSet": leads_set_n,
            "membershipsSold": memberships_sold,
        },
        "hours": [hrs[k] for k in sorted(hrs, key=int)],
        "jobs": cards,
        "activity": feed,
        "updated": now_s,
    }
    return payload


# ── publish (local only — no git, no cloud) ─────────────────────────────────

def publish(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs", "updated")}
    old_stable = None
    if OUT.exists():
        try:
            oldj = json.loads(OUT.read_text(encoding="utf-8"))
            old_stable = {k: v for k, v in oldj.items() if k not in ("generated", "generatedMs", "updated")}
        except Exception:
            pass
    if stable == old_stable and not force_heartbeat:
        return "unchanged"
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, OUT)
    return "written"


# ── cloud transport: GitHub contents API on the dashboard repo (PAT = DASHBOARD_TOKEN) ──

_GH_SHAS = {}
def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "service-livefeed",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def gh_fetch(path):
    try:
        j = _gh_req(path)
        _GH_SHAS[path] = j["sha"]
        return base64.b64decode(j["content"]).decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def gh_put(path, text, msg):
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(text.encode("utf-8")).decode()}
    if _GH_SHAS.get(path):
        body["sha"] = _GH_SHAS[path]
    try:
        j = _gh_req(path, "PUT", body)
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):          # sha raced (another writer) — refresh and retry once
            gh_fetch(path)
            if _GH_SHAS.get(path):
                body["sha"] = _GH_SHAS[path]
            j = _gh_req(path, "PUT", body)
        else:
            raise
    _GH_SHAS[path] = j["content"]["sha"]


def cloud_seed_state():
    """Start a relay session from the state the previous session committed."""
    txt = gh_fetch("servicefeed_state.json")
    if txt:
        try:
            STATE.write_text(txt, encoding="utf-8")
        except Exception:
            pass
    gh_fetch("servicefeed.json")          # prime the sha cache for the first put


def publish_cloud(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs", "updated")}
    if stable == publish_cloud._last and not force_heartbeat:
        return "unchanged"
    gh_put("servicefeed.json", json.dumps(payload, separators=(",", ":")),
           "Service feed " + payload["generated"])
    gh_put("servicefeed_state.json", STATE.read_text(encoding="utf-8"),
           "Service feed state " + payload["generated"])
    publish_cloud._last = stable
    return "pushed"
publish_cloud._last = None


def arm_next():
    """Queue the successor relay run. GitHub's native cron skips ticks, so every
    session arms its own replacement; the workflow's concurrency group collapses
    extra pending runs."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/johnschwinghamer94-lab/sierra-ropp-hourly/actions/workflows/servicefeed.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
                     "Accept": "application/vnd.github+json", "User-Agent": "service-livefeed",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        log("armed successor relay run")
    except Exception as ex:
        log("WARN: could not arm successor: " + repr(ex)[:150])


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE)


def cycle(dry=False, force_heartbeat=False):
    state = load_state()
    payload = build(state)
    if dry:
        print(json.dumps(payload, indent=2)[:4000])
        return "dry"
    save_state(state)
    if CLOUD:
        return publish_cloud(payload, force_heartbeat)
    return publish(payload, force_heartbeat)


def in_window(now):
    return (now.hour, now.minute) >= DAY_START and (now.hour, now.minute) <= DAY_END


def cloud_main():
    """One relay session on a GitHub Actions runner (TZ=America/Los_Angeles set
    by the workflow so datetime.now() is Vegas time). Concurrency is handled by
    the workflow's concurrency group — no PID lock here."""
    now = datetime.now()
    if (now.hour, now.minute) < (6, 35) or (now.hour, now.minute) > DAY_END:
        log("outside ops window — session exits")
        return
    fp = Path.home() / ".servicetitan" / "sierra.json"
    if not fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(os.environ["ST_CREDS_JSON"])
    cloud_seed_state()
    log("cloud session started (cap %d min)" % MAX_MIN)
    arm_next()                        # successor waits in the queue from minute one
    t0 = time.time()
    last_push = 0.0
    while True:
        now = datetime.now()
        if (now.hour, now.minute) > DAY_END:
            log("past %02d:%02d — session done for today" % DAY_END)
            break
        if (time.time() - t0) / 60 > MAX_MIN:
            log("session cap reached — handing off to the next relay run")
            if (now.hour, now.minute) < DAY_END:
                arm_next()            # belt & suspenders: re-arm on the way out too
            break
        if in_window(now):
            try:
                hb = time.time() - last_push > 240
                r = cycle(force_heartbeat=hb)
                if r == "pushed":
                    last_push = time.time()
                log("cycle -> " + str(r))
            except Exception as ex:
                log("cycle ERROR: " + repr(ex)[:300])
        time.sleep(CYCLE_SECS)


def main():
    once = "--once" in sys.argv
    dry = "--dry" in sys.argv
    if dry or once:
        t0 = time.time()
        result = cycle(dry=dry, force_heartbeat=True)
        log("single cycle -> " + str(result) + " (%.1fs)" % (time.time() - t0))
        return
    if CLOUD:
        cloud_main()
        return
    log("loop started (pid %d)" % os.getpid())
    last_push = 0.0
    while True:
        now = datetime.now()
        if (now.hour, now.minute) > DAY_END:
            log("past %02d:%02d — loop done for today" % DAY_END)
            break
        if in_window(now):
            try:
                hb = time.time() - last_push > 240
                r = cycle(force_heartbeat=hb)
                if r == "written":
                    last_push = time.time()
                log("cycle -> " + str(r))
            except Exception as ex:
                log("cycle ERROR: " + repr(ex)[:300])
        time.sleep(CYCLE_SECS)


if __name__ == "__main__":
    main()
