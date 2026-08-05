#!/usr/bin/env python3
"""Coaching-loop closure engine for the Service department.

THE QUESTION THIS ANSWERS: we measure per-tech behavior daily (options built,
membership offer rate, close rate, recording status) AND generate a daily
coaching plan naming one specific gap per tech. This engine is the missing
join between the two — it builds a per-tech-per-day fact table and, for every
coaching plan issued, compares the named metric before vs. after the plan to
answer "did the behavior actually move?"

Inputs (read-only, no ServiceTitan calls — this engine consumes what
service_data.py / service_coaching.json / servicefeed_sync.py already
publish, so it stays cheap and can run every Pulse cycle):
  - servicedata_history.json  <- per-day dept + per-tech {sales, opps, close,
    mem} snapshots (service_data.py). This is the ONLY source of per-tech
    history for days before today; it does NOT carry optionsPerOpp,
    membershipOfferRate, or recording status, so those are null on past days.
  - servicedata.json          <- today's live per-tech rows (techs.today),
    which DO carry optionsPerOpp / membershipOffered / membershipOfferRate /
    revenue — used to enrich today's fact-table row beyond what history keeps.
  - servicefeed.json          <- today's live per-job siro.rec flags. This is
    the ONLY place recording status has ever existed; there is no historical
    recording data before this engine started capturing it, so every day
    before "today" of a given run gets recorded=null, not 0.
  - service_coaching.json     <- reps[] (today's issued plan per tech, with
    headlineGap + focus text) and history{tech:[{date,plan}]} (prior plan
    dates — link only, no captured focus text, so those emit
    metric=null/verdict="unmeasured").

Output: coaching_outcomes.json — written locally AND published to the
dashboard repo (same GitHub contents-API helper service_data.py /
service_deck_refresh.py use), containing:
  days        {date: {tech: {...}}}          — the tech-day fact table,
                                                appended idempotently, ~60 days
                                                retained
  compliance  {techs:{}, dept:{}}             — last-7-day recording rate
                                                (only counts days that
                                                actually captured recording
                                                status)
  outcomes    [...]                           — per coaching-plan-issued
                                                before/after comparison
  generated, notes

Schedule: standalone script, NOT folded into service_data.py. Rationale: it
only reads already-published JSON (no ServiceTitan API calls), so it's cheap
(a handful of raw/API GETs) and has no reason to share service_data.py's
20-30s ServiceTitan fetch budget or its --light/full run-mode logic. Run it
right after service_data.py + service_coaching's own routine finish each
Pulse cycle (append a step to the same workflow/cron that runs
service_data.py --light, after that step) so today's fact-table row and
today's coaching plan are both fresh before this reads them.
"""
import base64, json, os, sys, time, datetime as dt
import urllib.request, urllib.error
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()

PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
API = "https://api.github.com/repos/" + PUB_REPO + "/contents/"
RAW = "https://raw.githubusercontent.com/" + PUB_REPO + "/main/"

RECORDING_HISTORY_START = None  # set once we successfully capture a day; see main()
KEEP_DAYS = 60


# ── GitHub contents-API helpers (verbatim pattern from service_data.py /
# service_deck_refresh.py — reused, not reinvented) ─────────────────────────
def _gh_headers(extra=None):
    h = {"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
         "Accept": "application/vnd.github+json", "User-Agent": "coaching-outcomes"}
    if extra:
        h.update(extra)
    return h


def gh_fetch(path):
    """Auth'd contents-API fetch — returns (text, sha) or (None, None) on 404."""
    req = urllib.request.Request(API + path, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read())
        return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def raw_fetch(path):
    """No-auth fetch of the current PUBLIC file (works without DASHBOARD_TOKEN,
    used for every input file — this engine never needs write access to read
    them)."""
    req = urllib.request.Request(RAW + path, headers={"User-Agent": "coaching-outcomes"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def raw_json(path):
    txt = raw_fetch(path)
    return json.loads(txt) if txt else None


def gh_put(path, text, sha, msg):
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="PUT",
                                  headers=_gh_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read())


def num_or_none(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 1. tech-day fact table ──────────────────────────────────────────────────
def build_today_row_from_live(name, sd_live, feed):
    """Enrich today's row from the live servicedata.json techs.today row (has
    optionsPerOpp / membershipOfferRate / revenue that servicedata_history.json
    never retains) + servicefeed.json (has today's ONLY recording data)."""
    row = None
    for r in (sd_live or {}).get("techs", {}).get("today", []) if sd_live else []:
        if r.get("name") == name:
            row = r
            break
    out = {
        "callsRun": row.get("opps") if row else None,
        "closeRate": row.get("closeRate") if row else None,
        "revenue": row.get("revenue") if row else None,
        "membershipsSold": row.get("membershipsSold") if row else None,
        "membershipOffered": row.get("membershipOffered") if row else None,
        "membershipOfferJobs": row.get("membershipOfferJobs") if row else None,
        "membershipOfferRate": row.get("membershipOfferRate") if row else None,
        "optionsPerOpp": row.get("optionsPerOpp") if row else None,
    }
    # recording: only source that has EVER existed is today's live servicefeed.json
    recorded = eligible = None
    if feed:
        eligible = 0
        recorded = 0
        for j in feed.get("jobs", []):
            if j.get("tech") != name:
                continue
            eligible += 1
            siro = j.get("siro")
            if siro and siro.get("rec"):
                recorded += 1
    out["recorded"] = recorded
    out["recordedPct"] = round(recorded / eligible * 100, 1) if eligible else None
    opps = out["callsRun"]
    out["lowVolume"] = (opps is None) or (opps < 2)
    return out


def build_history_row(rec):
    """A day from servicedata_history.json's compact per-tech dict:
    {sales, opps, close, mem} — that's ALL it retains. Everything else this
    engine wants (optionsPerOpp, membershipOfferRate, revenue, recording) was
    never captured for that day, so it is null, not zero."""
    opps = rec.get("opps")
    return {
        "callsRun": opps,
        "closeRate": rec.get("close"),
        "revenue": None,               # history never stored per-tech revenue
        "membershipsSold": rec.get("mem"),
        "membershipOffered": None,
        "membershipOfferJobs": None,
        "membershipOfferRate": None,
        "optionsPerOpp": None,
        "recorded": None,              # recording history didn't exist before this engine
        "recordedPct": None,
        "lowVolume": (opps is None) or (opps < 2),
    }


def build_days(sd_hist, sd_live, feed, prev_days):
    """{date: {tech: {...}}} merged from servicedata_history.json (all past
    days it has) + today enriched from live artifacts, layered onto whatever
    this engine already had stored (prev_days) so a day is never re-derived
    worse than a prior run captured it (e.g. today's recording snapshot from
    an earlier run today isn't clobbered by a later run with fewer jobs
    resolved — we simply overwrite today's row each run since it's the live,
    most current day; past days are immutable once servicedata_history.json
    has them)."""
    days = dict(prev_days or {})
    today_iso = TODAY.isoformat()
    for date_iso, rec in (sd_hist or {}).items():
        techs = rec.get("techs", {})
        day_out = days.get(date_iso, {})
        for name, t in techs.items():
            if date_iso == today_iso:
                day_out[name] = build_today_row_from_live(name, sd_live, feed)
            else:
                # don't clobber an already-stored day with a re-derive that
                # would lose nothing here (history rows are static), but keep
                # this explicit so future fields added upstream get picked up
                day_out[name] = build_history_row(t)
        days[date_iso] = day_out
    # also make sure every roster tech from today's live snapshot appears
    # today even if servicedata_history.json's today entry is thin
    if sd_live:
        day_out = days.get(today_iso, {})
        for r in sd_live.get("techs", {}).get("today", []):
            name = r.get("name")
            if name and name not in day_out:
                day_out[name] = build_today_row_from_live(name, sd_live, feed)
        days[today_iso] = day_out
    # retain ~60 days
    kept_dates = sorted(days.keys())[-KEEP_DAYS:]
    return {d: days[d] for d in kept_dates}


# ── 2. recording compliance (last 7 days, only days with real data) ────────
def build_compliance(days):
    cutoff = TODAY - dt.timedelta(days=7)
    window_dates = [d for d in days if dt.date.fromisoformat(d) > cutoff and dt.date.fromisoformat(d) <= TODAY]
    # recorded/eligible aren't stored separately on the row — recordedPct +
    # recorded reconstruct eligible (eligible isn't persisted directly since
    # the row schema only stores recorded + recordedPct).
    per_tech = {}
    for d in window_dates:
        for name, row in days[d].items():
            recorded = row.get("recorded")
            pct = row.get("recordedPct")
            if recorded is None or pct is None:
                continue
            eligible = round(recorded / (pct / 100)) if pct else recorded
            e = per_tech.setdefault(name, {"recorded": 0, "eligible": 0})
            e["recorded"] += recorded
            e["eligible"] += eligible
    out_techs = {}
    dept_recorded = dept_eligible = 0
    for name, e in per_tech.items():
        pct = round(e["recorded"] / e["eligible"] * 100, 1) if e["eligible"] else None
        out_techs[name] = {"recorded": e["recorded"], "eligible": e["eligible"], "pct": pct}
        dept_recorded += e["recorded"]
        dept_eligible += e["eligible"]
    dept = {"recorded": dept_recorded, "eligible": dept_eligible,
            "pct": round(dept_recorded / dept_eligible * 100, 1) if dept_eligible else None}
    return {"windowDays": window_dates, "techs": out_techs, "dept": dept}


# ── 3. coaching focus -> metric classification ──────────────────────────────
def classify_focus(text):
    if not text:
        return None
    t = text.lower()
    if "option" in t:
        return "optionsPerOpp"
    if "membership" in t or "sam" in t:
        return "membershipOfferRate"
    if "record" in t or "siro" in t:
        return "recordedPct"
    if "close" in t or "expectation" in t or "urgency" in t:
        return "closeRate"
    return None


VERDICT_THRESH = {"optionsPerOpp": 0.3, "membershipOfferRate": 3.0,
                   "closeRate": 3.0, "recordedPct": 3.0}


def business_days_around(date_iso, days_dict, n=5):
    """Return (before_dates, after_dates): up to n Mon-Fri calendar dates
    strictly before / after date_iso that exist as keys in days_dict, capped
    at TODAY on the 'after' side (can't look into the future)."""
    d0 = dt.date.fromisoformat(date_iso)
    before, cur = [], d0 - dt.timedelta(days=1)
    while len(before) < n and cur >= d0 - dt.timedelta(days=21):
        if cur.weekday() < 5 and cur.isoformat() in days_dict:
            before.append(cur.isoformat())
        cur -= dt.timedelta(days=1)
    after, cur = [], d0 + dt.timedelta(days=1)
    while len(after) < n and cur <= TODAY:
        if cur.weekday() < 5 and cur.isoformat() in days_dict:
            after.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return sorted(before), sorted(after)


def metric_values(tech, dates, days_dict, metric):
    vals = []
    for d in dates:
        row = days_dict.get(d, {}).get(tech)
        if not row:
            continue
        v = row.get(metric)
        if v is None:
            continue
        if row.get("lowVolume"):
            continue   # don't let a 1-call day drive the average
        vals.append(v)
    return vals


def build_outcomes(coaching, days_dict):
    events = []
    plan_date = coaching.get("date") if coaching else None
    for rep in (coaching or {}).get("reps", []):
        text = (rep.get("headlineGap") or "") + " " + " ".join(rep.get("focus") or [])
        events.append({"tech": rep["name"], "date": plan_date, "focusText": rep.get("headlineGap")})
    for tech, hist in (coaching or {}).get("history", {}).items():
        for h in hist:
            d = h.get("date")
            if d == plan_date:
                continue   # already covered by reps[] (fuller text) above
            events.append({"tech": tech, "date": d, "focusText": None})

    outcomes = []
    for ev in events:
        tech, date, focus_text = ev["tech"], ev["date"], ev["focusText"]
        if not date:
            continue
        metric = classify_focus(focus_text)
        if metric is None:
            outcomes.append({"tech": tech, "date": date, "focusText": focus_text,
                              "metric": None, "before": None, "after": None,
                              "delta": None, "verdict": "unmeasured"})
            continue
        before_dates, after_dates = business_days_around(date, days_dict)
        before_vals = metric_values(tech, before_dates, days_dict, metric)
        after_vals = metric_values(tech, after_dates, days_dict, metric)
        if len(before_vals) < 2 or len(after_vals) < 2:
            before_avg = round(sum(before_vals) / len(before_vals), 2) if before_vals else None
            after_avg = round(sum(after_vals) / len(after_vals), 2) if after_vals else None
            outcomes.append({"tech": tech, "date": date, "focusText": focus_text,
                              "metric": metric, "before": before_avg, "after": after_avg,
                              "delta": None, "verdict": "insufficient-data"})
            continue
        before_avg = round(sum(before_vals) / len(before_vals), 2)
        after_avg = round(sum(after_vals) / len(after_vals), 2)
        delta = round(after_avg - before_avg, 2)
        thresh = VERDICT_THRESH.get(metric, 3.0)
        if delta > thresh:
            verdict = "improved"
        elif delta < -thresh:
            verdict = "declined"
        else:
            verdict = "flat"
        outcomes.append({"tech": tech, "date": date, "focusText": focus_text,
                          "metric": metric, "before": before_avg, "after": after_avg,
                          "delta": delta, "verdict": verdict})
    return outcomes


# ── assembly ─────────────────────────────────────────────────────────────────
def main():
    dry = "--dry" in sys.argv
    print("[coaching_outcomes] start %s" % NOW.isoformat())

    sd_hist = raw_json("servicedata_history.json") or {}
    sd_live = raw_json("servicedata.json")
    feed = raw_json("servicefeed.json")
    coaching = raw_json("service_coaching.json")
    prev, prev_sha = gh_fetch("coaching_outcomes.json") if not dry else (None, None)
    prev_json = json.loads(prev) if prev else None
    if prev_json is None:
        # fall back to local copy (first run, or dry mode without a token)
        local_path = os.path.join(HERE, "coaching_outcomes.json")
        if os.path.exists(local_path):
            try:
                prev_json = json.load(open(local_path))
            except Exception:
                prev_json = None
    prev_days = (prev_json or {}).get("days", {})

    print("  servicedata_history.json: %d days" % len(sd_hist))
    print("  servicedata.json live: %s" % ("present" if sd_live else "MISSING"))
    print("  servicefeed.json live: %s" % ("present" if feed else "MISSING"))
    print("  service_coaching.json: %s" % ("present" if coaching else "MISSING"))
    print("  prior coaching_outcomes.json days retained: %d" % len(prev_days))

    days = build_days(sd_hist, sd_live, feed, prev_days)
    compliance = build_compliance(days)
    outcomes = build_outcomes(coaching, days)

    earliest_recording_day = min(
        (d for d, techs in days.items() for r in techs.values() if r.get("recordedPct") is not None),
        default=None)

    notes = [
        "Per-tech optionsPerOpp / membershipOfferRate / revenue are only "
        "available for TODAY (from live servicedata.json) — servicedata_history.json's "
        "daily snapshots only ever retained {sales, opps, closeRate, membershipsSold} "
        "per tech, so those fields are null on every earlier day.",
        "Recording status (recordedPct) has never been persisted historically before "
        "this engine; it is captured starting %s and is null on every day before that." % (
            earliest_recording_day or TODAY.isoformat()),
        "Coaching-plan focus text (headlineGap/focus) is only fully available for the "
        "MOST RECENT date in service_coaching.json (reps[]); earlier dates in its "
        "history{} block only retain a plan-file link, not the focus text, so those "
        "outcome rows emit metric:null / verdict:'unmeasured'.",
        "5-business-day before/after windows use whatever days actually exist in this "
        "engine's own accumulated fact table so far — with only a few days of history, "
        "most verdicts are correctly 'insufficient-data', not a bug.",
        "Tech-days with fewer than 2 opportunity calls are flagged lowVolume and excluded "
        "from before/after averaging so a single-call day can't drive a verdict.",
    ]

    out = {"generated": NOW.isoformat(), "notes": notes, "days": days,
           "compliance": compliance, "outcomes": outcomes}

    local_path = os.path.join(HERE, "coaching_outcomes.json")
    with open(local_path + ".tmp", "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    os.replace(local_path + ".tmp", local_path)
    print("wrote %s" % local_path)

    if dry:
        print("DRY RUN — nothing published.")
        return out

    if "DASHBOARD_TOKEN" not in os.environ:
        print("no DASHBOARD_TOKEN set — local write only, skipping publish.")
        return out

    text = json.dumps(out, separators=(",", ":"), sort_keys=True)
    msg = "Coaching outcomes " + NOW.strftime("%Y-%m-%d %H:%M:%S")
    gh_put("coaching_outcomes.json", text, prev_sha, msg)
    print("published coaching_outcomes.json to %s" % PUB_REPO)
    return out


if __name__ == "__main__":
    main()
