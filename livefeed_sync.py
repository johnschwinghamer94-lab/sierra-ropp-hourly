#!/usr/bin/env python3
"""SILO Live Feed sync — per-job lifecycle tracker for the LIVE FEED dashboard tab.

Modeled on the plumbing Live Call Tracker's feed, but for the SILO team (HVAC):
every job today with a SILO tech on it, tracked through
    DISPATCHED -> ON SITE -> OPTIONS ($) -> SIGNED ($) -> DONE
plus a live activity feed and needs-attention warnings.

Entity APIs only (never the rate-limited reporting API):
    jpm/appointments + jobs, dispatch/appointment-assignments, crm/customers,
    sales/estimates (options = active unsold; signed = soldOn today).

Writes livefeed.json into the pages repo and pushes (skip-if-unchanged with a
10-min heartbeat so the page's staleness meter keeps moving). Stage times are
observed at transition time and persisted in livefeed_state.json; estimate
times (options/signed) are real API timestamps. First run of a day back-fills
already-passed stages without times (shown as checkmarks) — live timestamps
accumulate from then on.

Runs in TWO environments (same file, keep private repo & servicetitan/ in sync):
  Windows (default): publishes by git commit/push from the local repo clone.
  Cloud (LIVEFEED_CLOUD=1, GitHub Actions): publishes livefeed.json +
    livefeed_state.json to the dashboard repo via the GitHub contents API with
    DASHBOARD_TOKEN (same pattern as graph_hourly.py — PAT commits trigger the
    Pages deploy; GITHUB_TOKEN commits would NOT). State is seeded from the repo
    at session start so 5-hour relay sessions hand off cleanly. Creds come from
    ST_CREDS_JSON (st_client materializes ~/.servicetitan/sierra.json).

Usage:
    python livefeed_sync.py            # loop: one cycle / 90 s, 06:50-22:00, exits after
    python livefeed_sync.py --once     # single cycle now (ignores the time window)
    python livefeed_sync.py --dry      # single cycle, print JSON, no write/git
"""
import base64, json, os, sys, time, subprocess, urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import st_client as st

CLOUD = os.environ.get("LIVEFEED_CLOUD") == "1"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = Path(os.environ.get("LIVEFEED_REPO", r"C:\Users\johns\sierra-ropp-dashboard"))
OUT = (SCRIPT_DIR if CLOUD else REPO) / "livefeed.json"
STATE = SCRIPT_DIR / "livefeed_state.json"
LOCK = SCRIPT_DIR / "livefeed.lock"
LOG = SCRIPT_DIR / "livefeed_log.txt"

# cloud publish target + session cap (GitHub Actions jobs die at 6 h)
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
MAX_MIN = int(os.environ.get("LIVEFEED_MAX_MIN", "310"))

TAG_ROPP = 962027                 # "ROPP" tag type
TAG_MGMT_REMOVED = 545867780      # "Management Removed ROPP" tag type

CYCLE_SECS = 90
DAY_START = (6, 50)     # local (PC is Vegas time)
DAY_END = (22, 0)       # techs regularly work past 8 PM in season — track them

# SILO roster — prefer the engine's curated list so roster edits propagate.
try:
    from UPDATE_DASHBOARD import SILO as ROSTER
except Exception:
    ROSTER = ["Alex - Oleksiy Yakovchuk", "Noah Weng", "Joe Mendoza", "Benjamin Wyllie",
              "Nikko April", "Andrew Trujillo", "Dustin Romine", "Juan Tlatenchi",
              "Brandon Moreno", "Francisco Valencia", "Mario Castro", "Cole Pantol",
              "Nathan Colquitt", "Robert Silinzy", "Andrew Alonso"]

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
    """ServiceTitan UTC timestamp -> aware local datetime (PC is Vegas time)."""
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

# ── one polling cycle ─────────────────────────────────────────────────────────

_LOOKUPS = {}
def _lookups(today):
    """Business-unit + job-type + tag-type name maps, fetched once per day (they
    don't churn intraday; saves 3 API calls on every cycle)."""
    if _LOOKUPS.get("date") != today.isoformat():
        _LOOKUPS["bus"] = {b["id"]: b.get("name", "") for b in paged("/settings/v2/tenant/{tenant}/business-units", {})}
        _LOOKUPS["jts"] = {t["id"]: t.get("name", "") for t in paged("/jpm/v2/tenant/{tenant}/job-types", {})}
        _LOOKUPS["tagnames"] = {t["id"]: t.get("name", "") for t in paged("/settings/v2/tenant/{tenant}/tag-types", {})}
        _LOOKUPS["emps"] = {t["id"]: t.get("name", "") for t in paged("/settings/v2/tenant/{tenant}/technicians", {})}
        _LOOKUPS["date"] = today.isoformat()
    return _LOOKUPS["bus"], _LOOKUPS["jts"], _LOOKUPS["tagnames"]

def flag_tags(tag_names):
    """Recall/warranty chips from a job's tag names (registration workflow tags excluded)."""
    out = []
    for nm in tag_names:
        low = (nm or "").lower()
        if low.startswith("recall"):
            n = nm[6:].replace("- RW", "").replace("RW", "").strip(" -  ")
            out.append({"k": "recall", "n": ("Recall " + n).strip()})
        elif "warranty" in low and "registration" not in low:
            out.append({"k": "warranty", "n": nm.split(" - ")[0].replace("(Nuve)", "").strip()})
    return out

def fetch_today():
    today = date.today()
    day0 = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc)
    day1 = day0 + timedelta(days=1)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

    appts = paged("/jpm/v2/tenant/{tenant}/appointments",
                  {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
    appts = [a for a in appts if a.get("status") not in ("Canceled",)]
    if not appts:
        return today, [], {}, {}, {}, [], {}, []

    # tech per appointment (SILO filter happens here)
    asg = []
    aids = [a["id"] for a in appts]
    for i in range(0, len(aids), 50):
        asg += paged("/dispatch/v2/tenant/{tenant}/appointment-assignments",
                     {"appointmentIds": ",".join(str(x) for x in aids[i:i + 50])})
        time.sleep(0.15)
    tech_by_appt = {}
    for a in asg:
        if a.get("active") and a.get("technicianName") in set(ROSTER):
            tech_by_appt.setdefault(a["appointmentId"], []).append(a["technicianName"])

    silo_appts = [a for a in appts if a["id"] in tech_by_appt]
    job_ids = sorted({a["jobId"] for a in silo_appts})
    if not job_ids:
        return today, [], {}, {}, {}, [], {}, []

    jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)}
    cust_ids = sorted({j.get("customerId") for j in jobs.values() if j.get("customerId")})
    custs = {c["id"]: c.get("name", "") for c in chunked_get("/crm/v2/tenant/{tenant}/customers", cust_ids)}

    bus, jts, tagnames = _lookups(today)
    for j in jobs.values():
        j["_bu"] = bus.get(j.get("businessUnitId"), "")
        j["_jt"] = jts.get(j.get("jobTypeId"), "")
        j["_tags"] = [tagnames.get(t, "") for t in (j.get("tagTypeIds") or [])]

    # estimates touched today (options) + sold today (money on the call job)
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"modifiedOnOrAfter": iso(day0)})
    time.sleep(0.15)
    ests += [e for e in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": iso(day0)})
             if e["id"] not in {x["id"] for x in ests}]
    est_by_job = {}
    for e in ests:
        if e.get("jobId") in jobs:
            est_by_job.setdefault(e["jobId"], []).append(e)

    # TGL CREATED — the authoritative signal: a lead job created today whose
    # jobGeneratedLeadSource points at one of our call jobs. (A sold estimate on
    # the call job is NOT a TGL — techs also sell parts that way.)
    # any-tech attribution for the bonus sheet: job -> techs from ALL of today's
    # assignments (asg covers every appointment today, not just SILO)
    all_job_tech = {}
    for a in asg:
        if a.get("active") and a.get("technicianName") and a.get("jobId"):
            all_job_tech.setdefault(a["jobId"], []).append(a["technicianName"])

    lead_by_src = {}
    dept_leads = []      # every TGL created today, department-wide (bonus sheet)
    emps = _LOOKUPS.get("emps", {})
    for lj in paged("/jpm/v2/tenant/{tenant}/jobs", {"createdOnOrAfter": iso(day0)}):
        gls = lj.get("jobGeneratedLeadSource") or {}
        src = gls.get("jobId")
        if not src:
            continue
        # Only Estimate-type jobs are TGL creations. Install jobs booked after a
        # sold TGL also carry jobGeneratedLeadSource — counting them double-pays
        # (burned: Joe 664141521 "Install 80% Horizontal TGL" on 7/11).
        if not (jts.get(lj.get("jobTypeId")) or "").startswith("Estimate"):
            continue
        if src in jobs:
            ent = lead_by_src.setdefault(src, {"n": 0, "t": None, "ids": []})
            ent["n"] += 1
            ent["ids"].append(lj["id"])
            dtl = parse_utc(lj.get("createdOn"))
            if dtl and ent["t"] is None:
                ent["t"] = fmt_t(dtl)
        # credited tech: ST's own lead-source employee first, else source-job tech
        tech = emps.get(gls.get("employeeId")) or (all_job_tech.get(src) or [None])[0]
        dtl = parse_utc(lj.get("createdOn"))
        dept_leads.append({"id": lj["id"], "number": lj.get("jobNumber", ""),
                           "src": src, "tech": tech,
                           "t": fmt_t(dtl) if dtl else ""})

    return today, silo_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src, dept_leads

def build(state):
    today, silo_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src, dept_leads = fetch_today()
    now = datetime.now().astimezone()
    now_s = fmt_t(now)
    dkey = today.isoformat()

    first_run_of_day = state.get("date") != dkey
    if first_run_of_day:
        keep_sheet = state.get("sheet", {})
        state.clear()
        state.update({"date": dkey, "jobs": {}, "feed": [], "sheet": keep_sheet})
    seen = state["jobs"]
    feed = state["feed"]

    def event(icon, text, color):
        if not first_run_of_day:
            feed.insert(0, {"i": icon, "x": text, "t": now_s, "c": color})

    # one card per job (primary appt = earliest non-canceled today)
    by_job = {}
    for a in sorted(silo_appts, key=lambda x: x.get("start") or ""):
        by_job.setdefault(a["jobId"], a)

    RANK = {"Working": 0, "Dispatched": 1, "Hold": 2, "Scheduled": 3, "Done": 4}
    cards = []
    on_site = en_route = completed = 0
    opt_total = opt_count = 0
    opt_jobs = set()
    signed_total = 0.0
    signed_jobs = set()

    for jid, appt in by_job.items():
        j = jobs.get(jid, {})
        techs = sorted(set(sum((tech_by_appt.get(a["id"], []) for a in silo_appts if a["jobId"] == jid), [])))
        cust = custs.get(j.get("customerId"), "") or "—"
        status = appt.get("status") or "Scheduled"
        if j.get("jobStatus") == "Completed" or status == "Done":
            status = "Done"
        js = seen.setdefault(str(jid), {"st": {}, "opt": 0.0, "sold": 0.0, "status": ""})

        # observed stage times (persisted at first sight of each status)
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

        # estimates on this job: unsold = options in play; sold = money closed on
        # the call itself (parts etc.) — a sold estimate is NOT a TGL signal,
        # the lead-job link (lead_by_src) is the authority for TGLs.
        opts_t = opts_n = 0
        opt_time = sold_time = None
        sold_t = 0.0
        for est in est_by_job.get(jid, []):
            sub = float(est.get("subtotal") or 0)
            st_name = ((est.get("status") or {}).get("name") or "").lower()
            if est.get("soldOn") or st_name == "sold":
                sold_t += sub
                dtl = parse_utc(est.get("soldOn"))
                if dtl and dtl.date() == today:
                    sold_time = fmt_t(dtl)
            elif est.get("active") and st_name != "dismissed" and sub > 0:
                opts_t += sub
                opts_n += 1
                dtl = parse_utc(est.get("createdOn"))
                if dtl:
                    opt_time = fmt_t(dtl)
        lead = lead_by_src.get(jid)
        tgl_n = lead["n"] if lead else 0
        tgl_time = lead["t"] if lead else None
        if opts_t > js.get("opt", 0) + 0.5:
            event("\U0001F6E0", "Building options: " + ", ".join(techs) + " @ " + cust +
                  " [$" + format(int(opts_t), ",") + "]", "#F5B324")
        prev_tgl = js.get("tglN")
        if prev_tgl is None:
            js["tglN"] = tgl_n            # key added mid-day: adopt silently, no catch-up burst
        elif tgl_n > prev_tgl:
            event("✅", "TGL CREATED: " + ", ".join(techs) + " @ " + cust +
                  (" [×" + str(tgl_n) + "]" if tgl_n > 1 else ""), "#4ADE80")
        if sold_t > js.get("sold", 0) + 0.5:
            event("\U0001F4B5", "Sold on call: " + ", ".join(techs) + " @ " + cust +
                  " [+$" + format(int(sold_t - js.get("sold", 0)), ",") + "]", "#7fb3e8")
        js["opt"], js["sold"], js["tglN"], js["status"] = opts_t, sold_t, tgl_n, status

        # warnings while on site
        warn = None
        if status == "Working":
            t_on = js["st"].get("onsite")
            mins = None
            if t_on and t_on != "✓":
                try:
                    t0 = datetime.strptime(t_on[:-1] + ("PM" if t_on.endswith("p") else "AM"), "%I:%M%p")
                    t0 = now.replace(hour=t0.hour, minute=t0.minute, second=0)
                    mins = int((now - t0).total_seconds() / 60)
                except Exception:
                    pass
            if mins is not None and mins > 120:
                warn = "⏰ on site " + str(mins) + "m"
            elif mins is not None and mins > 90 and opts_n == 0 and tgl_n == 0 and sold_t == 0:
                warn = "⚠ no options after " + str(mins) + "m"

        if status == "Working":
            on_site += 1
        elif status == "Dispatched":
            en_route += 1
        elif status == "Done":
            completed += 1
        if opts_n:
            opt_total += opts_t; opt_count += opts_n; opt_jobs.add(jid)
        if tgl_n:
            signed_jobs.add(jid)
        signed_total += sold_t

        start_l = parse_utc(appt.get("start"))
        done_l = parse_utc(j.get("completedOn"))     # real API timestamp when present
        jtags = j.get("tagTypeIds") or []
        tag_flag = ("removed" if TAG_MGMT_REMOVED in jtags
                    else None if TAG_ROPP in jtags else "noropp")
        cards.append({
            "jobId": jid, "jobNumber": j.get("jobNumber", ""),
            "tech": ", ".join(techs), "customer": cust,
            "bu": j.get("_bu", ""), "jobType": j.get("_jt", ""),
            "status": status, "rank": RANK.get(status, 3),
            "start": fmt_t(start_l) if start_l else "",
            "startIso": appt.get("start") or "9999",
            "stages": {
                "dispatched": js["st"].get("dispatched"),
                "onsite": js["st"].get("onsite"),
                "options": {"t": opt_time, "n": opts_n, "total": int(opts_t)} if opts_n else None,
                "signed": {"t": tgl_time, "n": tgl_n} if tgl_n else None,
                "sold": {"t": sold_time, "total": int(sold_t)} if sold_t > 0.5 else None,
                "done": (fmt_t(done_l) if done_l else None) or js["st"].get("done"),
            },
            "warn": warn,
            "tagFlag": tag_flag,
            "extraTags": flag_tags(j.get("_tags", [])),
        })

    # board reads top-to-bottom, left-to-right in call-start order (John's spec)
    cards.sort(key=lambda c: c["startIso"])

    # ── bonus sheet: add every dept TGL (Estimate-type, minus SHEET_EXCLUDE)
    #    with the SOURCE CALL job# in column C, then keep D (CA ran Y/N) and
    #    E (sold Y/N) updated for 7 days as the TGL runs and sells ──────────
    sheet = state.setdefault("sheet", {})
    for L in dept_leads:
        k = str(L["id"])
        if k in sheet:
            continue
        tech = L["tech"]
        if tech and tech in SHEET_EXCLUDE:
            sheet[k] = {"skip": True, "day": dkey}
            continue
        sd = lead_sameday(L["id"], dkey)
        ok = sheet_log({"date": dkey, "time": L["t"] or now_s,
                        "tech": tech or "", "first": sheet_name(tech or ""),
                        "jobId": L["src"], "jobNumber": str(L["src"]),
                        "srcId": L["src"], "sameDay": sd})
        if ok:
            sheet[k] = {"skip": False, "day": dkey, "src": L["src"],
                        "ran": "", "sold": "", "sd": sd}
    track = {int(k): v for k, v in sheet.items()
             if not v.get("skip") and v.get("src")
             and v.get("day", "") >= (today - timedelta(days=10)).isoformat()
             and not v.get("can")
             and not (v.get("ran") in ("Y", "N") and v.get("sold") == "Y")}
    if track and os.environ.get("SHEET_WEBHOOK", "").strip():
        lead_jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", list(track.keys()))}
        wk = (datetime.combine(today - timedelta(days=10), datetime.min.time())
              .astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        sold_ids = set()
        for e2 in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": wk}):
            if e2.get("jobId") in lead_jobs and (e2.get("soldOn") or ((e2.get("status") or {}).get("name") == "Sold")):
                sold_ids.add(e2["jobId"])
        for lid, tr in track.items():
            lj = lead_jobs.get(lid)
            if not lj:
                continue
            stj = lj.get("jobStatus")
            canceled = stj == "Canceled"    # F=CANCELED on the sheet, D/E stay blank
            ca = lead_ca(lid)
            if ca and ca.split()[0].upper() == "RYAN":
                ran = "RYAN EMAIL"          # Ryan works his TGL tickets by email
            else:
                ran = "Y" if stj == "Completed" else ""
            sold = "Y" if lid in sold_ids else ("N" if stj == "Completed" else "")
            sd = tr.get("sd")
            if sd is None:
                sd = lead_sameday(lid, tr.get("day", dkey))
            if (ran != tr.get("ran", "") or sold != tr.get("sold", "")
                    or sd != tr.get("sd") or canceled != bool(tr.get("can"))):
                if sheet_log({"op": "update", "jobNumber": str(tr["src"]),
                              "ran": ran, "sold": sold, "sameDay": sd,
                              "canceled": canceled}):
                    tr["ran"], tr["sold"], tr["sd"], tr["can"] = ran, sold, sd, canceled
    cutoff = (today - timedelta(days=13)).isoformat()
    for k in [k for k, v in sheet.items() if v.get("day", "") < cutoff]:
        del sheet[k]
    sheet_audit(state, today)
    if first_run_of_day:
        feed.insert(0, {"i": "\U0001F4E1", "x": "Live Feed online — tracking " + str(len(cards)) +
                        " SILO job" + ("" if len(cards) == 1 else "s") + " today", "t": now_s, "c": "#2E78C7"})
    del feed[60:]
    state["date"] = dkey

    payload = {
        "date": dkey,
        "day": now.strftime("%A, %B %d").upper(),
        "generated": now_s,
        "generatedMs": int(time.time() * 1000),
        "tgls": sorted([{"t": L["t"], "first": sheet_name(L["tech"] or "") or "?",
                         "src": L["src"],
                         "mine": not (L["tech"] and L["tech"] in SHEET_EXCLUDE)}
                        for L in dept_leads], key=lambda x: x["t"]),
        "kpis": {
            "jobs": len(cards), "completed": completed,
            "onSite": on_site, "enRoute": en_route,
            "optionsTotal": int(opt_total), "optionsCount": opt_count, "optionsJobs": len(opt_jobs),
            "signedTotal": int(signed_total), "signedJobs": len(signed_jobs),
        },
        "jobs": cards,
        "feed": feed,
    }
    return payload

# ── publish ───────────────────────────────────────────────────────────────────

# cloud transport: GitHub contents API on the dashboard repo (PAT = DASHBOARD_TOKEN)
_GH_SHAS = {}
def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "silo-livefeed",
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

_LEAD_APPT = {}
def lead_sameday(lead_id, ref_iso):
    """True if the lead job's first appointment falls on ref_iso (its creation
    day) — SAME DAY $30 vs SCHEDULED $10. Cached; None = no appointment yet."""
    if lead_id not in _LEAD_APPT:
        try:
            r = st.api_get("/jpm/v2/tenant/{tenant}/appointments", {"jobId": lead_id, "pageSize": 1})
            d = r.get("data") or []
            dtl = parse_utc(d[0].get("start")) if d else None
            _LEAD_APPT[lead_id] = dtl.date().isoformat() if dtl else None
        except Exception:
            return None
    v = _LEAD_APPT[lead_id]
    return None if v is None else v == ref_iso

_LEAD_CA = {}
def lead_ca(lead_id):
    """CA assigned to the TGL ticket (cached once found; retried while empty —
    CAs often get assigned hours after the lead is created)."""
    if _LEAD_CA.get(lead_id):
        return _LEAD_CA[lead_id]
    try:
        r = st.api_get("/dispatch/v2/tenant/{tenant}/appointment-assignments",
                       {"jobId": lead_id, "pageSize": 10})
        names = [a.get("technicianName") for a in r.get("data", [])
                 if a.get("active") and a.get("technicianName")]
        if names:
            _LEAD_CA[lead_id] = names[0]
            return names[0]
    except Exception:
        pass
    return None

# sheet shows first names; the only non-obvious mapping on the roster
_SHEET_NAMES = {"Benjamin Wyllie": "BEN"}
def sheet_name(full):
    return _SHEET_NAMES.get(full) or (full.split()[0].upper() if full else "")

# These techs bonus under another manager — never logged to John's sheet.
SHEET_EXCLUDE = {"Andrew Alonso", "Brandon Moreno", "Cole Pantol", "Francisco Valencia",
                 "Mario Castro", "Nathan Colquitt", "Robert Silinzy"}

def sheet_log(row):
    """POST a TGL event to John's bonus-sheet Apps Script webhook (env
    SHEET_WEBHOOK; silently inert when unset). Fire-and-forget with one retry —
    a sheet hiccup must never stall the feed loop."""
    url = os.environ.get("SHEET_WEBHOOK", "").strip()
    if not url:
        return False
    data = json.dumps(row).encode()
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "silo-livefeed"})
            urllib.request.urlopen(req, timeout=15)
            log("bonus sheet: logged TGL for " + str(row.get("first") or row.get("tech", "?")))
            return True
        except Exception as ex:
            if attempt == 2:
                log("WARN: bonus sheet post failed: " + repr(ex)[:150])
            time.sleep(2)
    return False


def sheet_check(job_numbers):
    """Ask the sheet which of these job#s are missing from column C (op:check)."""
    url = os.environ.get("SHEET_WEBHOOK", "").strip()
    if not url:
        return None
    try:
        req = urllib.request.Request(url, data=json.dumps({"op": "check", "jobs": job_numbers}).encode(),
            method="POST", headers={"Content-Type": "application/json", "User-Agent": "silo-livefeed"})
        with urllib.request.urlopen(req, timeout=30) as r:
            t = r.read().decode()
        return json.loads(t) if t.startswith("[") else None
    except Exception as ex:
        log("WARN: sheet check failed: " + repr(ex)[:120])
        return None

def sheet_audit(state, today):
    """Daily 2nd check (John's spec): the official TGLs-Created report over a
    10-day window vs the sheet. Rows the report has but the sheet lacks get
    self-healed into the right date block; sheet rows the report lacks are
    logged as warnings (never deleted). One run per day, after 8:30 AM."""
    if not os.environ.get("SHEET_WEBHOOK", "").strip():
        return
    aud = state.setdefault("sheetAudit", {})
    if aud.get("done") == today.isoformat():
        return
    now = datetime.now()
    if (now.hour, now.minute) < (8, 30):
        return
    aud["done"] = today.isoformat()       # one attempt per day, even on failure
    try:
        import ropp_live
        frm = (today - timedelta(days=10)).isoformat()
        fields, rows = ropp_live._fetch_report("tgls_created", frm, today.isoformat())
        ji = fields.index("JobNumber")
        ti = next((i for i, f in enumerate(fields) if "echnician" in f), None)
        report = {}
        for r_ in rows:
            jn = str(r_[ji] or "").strip()
            if len(jn) >= 6 and jn.isdigit():
                report[jn] = str(r_[ti] or "") if ti is not None else ""
        mine = {jn: t for jn, t in report.items() if t not in SHEET_EXCLUDE}
        missing = sheet_check(sorted(mine))
        if missing is None:
            return
        healed = 0
        if missing:
            day0 = (datetime.combine(today - timedelta(days=11), datetime.min.time())
                    .astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            src_map = {}
            jts = _LOOKUPS.get("jts", {})
            for lj in paged("/jpm/v2/tenant/{tenant}/jobs", {"createdOnOrAfter": day0}):
                gls = lj.get("jobGeneratedLeadSource") or {}
                s_ = str(gls.get("jobId") or "")
                if s_ in missing and (jts.get(lj.get("jobTypeId")) or "").startswith("Estimate"):
                    src_map[s_] = lj
            for jn in missing:
                lj = src_map.get(jn)
                created = parse_utc(lj.get("createdOn")) if lj else None
                dk = created.date().isoformat() if created else today.isoformat()
                tech = mine.get(jn, "")
                sd = lead_sameday(lj["id"], dk) if lj else None
                if sheet_log({"date": dk, "time": fmt_t(created) if created else "",
                              "tech": tech, "first": sheet_name(tech),
                              "jobId": int(jn), "jobNumber": jn, "srcId": int(jn),
                              "sameDay": sd}):
                    healed += 1
                    if lj:
                        state.setdefault("sheet", {})[str(lj["id"])] = {
                            "skip": False, "day": dk, "src": int(jn),
                            "ran": "", "sold": "", "sd": sd}
        ours = {str(v.get("src")) for v in state.get("sheet", {}).values()
                if not v.get("skip") and v.get("src")}
        extra = sorted(ours - set(report))
        log("audit vs TGLs-Created: report %d (yours) | healed %d missing | %d logged-not-in-report%s"
            % (len(mine), healed, len(extra), (": " + ",".join(extra[:8])) if extra else ""))
    except Exception as ex:
        log("audit ERROR: " + repr(ex)[:250])

def arm_next():
    """Queue the successor relay run. GitHub's native cron skips ticks (burned
    twice on day one), so every session arms its own replacement; the workflow's
    concurrency group collapses extra pending runs."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/johnschwinghamer94-lab/sierra-ropp-hourly/actions/workflows/livefeed.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
                     "Accept": "application/vnd.github+json", "User-Agent": "silo-livefeed",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        log("armed successor relay run")
    except Exception as ex:
        log("WARN: could not arm successor: " + repr(ex)[:150])

def cloud_seed_state():
    """Start a relay session from the state the previous session committed."""
    txt = gh_fetch("livefeed_state.json")
    if txt:
        try:
            STATE.write_text(txt, encoding="utf-8")
        except Exception:
            pass
    gh_fetch("livefeed.json")             # prime the sha cache for the first put

def publish_cloud(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs")}
    if stable == publish_cloud._last and not force_heartbeat:
        return "unchanged"
    gh_put("livefeed.json", json.dumps(payload, separators=(",", ":")),
           "Live feed " + payload["generated"])
    gh_put("livefeed_state.json", STATE.read_text(encoding="utf-8"),
           "Live feed state " + payload["generated"])
    publish_cloud._last = stable
    return "pushed"
publish_cloud._last = None

def git(*args, check=True):
    return subprocess.run(["git", "-C", str(REPO)] + list(args),
                          capture_output=True, text=True, check=check)

def publish(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs")}
    old_stable = None
    if OUT.exists():
        try:
            oldj = json.loads(OUT.read_text(encoding="utf-8"))
            old_stable = {k: v for k, v in oldj.items() if k not in ("generated", "generatedMs")}
        except Exception:
            pass
    if stable == old_stable and not force_heartbeat:
        return "unchanged"
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, OUT)
    git("add", "livefeed.json")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return "no git delta"
    git("commit", "-q", "-m", "Live feed " + payload["generated"])
    if git("push", "-q", "origin", "main", check=False).returncode:
        git("pull", "--rebase", "-q", check=False)
        if git("push", "-q", "origin", "main", check=False).returncode:
            return "PUSH FAILED"
    return "pushed"

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
        log("single cycle -> " + str(cycle(dry=dry, force_heartbeat=True)))
        return
    if CLOUD:
        cloud_main()
        return
    # loop mode with a PID lock
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False)
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            if str(pid) in out:
                log("another loop is running (pid %d) — exiting" % pid)
                return
        except Exception:
            pass
    LOCK.write_text(str(os.getpid()))
    log("loop started (pid %d)" % os.getpid())
    last_push = 0.0
    try:
        while True:
            now = datetime.now()
            if (now.hour, now.minute) > DAY_END:
                log("past %02d:%02d — loop done for today" % DAY_END)
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
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass

if __name__ == "__main__":
    main()
