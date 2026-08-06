#!/usr/bin/env python3
"""Pipeline health checker — the dashboard system's own smoke alarm.

Every past failure (stale feeds, a dead credential, a 9-day-frozen coaching
metric, a wedged Pages deploy, a hung relay) was discovered by the owner
noticing a wrong number on screen, because the workflows that produce these
artifacts reported green even when their OUTPUT went stale. This script
inspects the PUBLISHED artifacts directly (the same files a human would look
at) and reports on freshness, independent of whether the producing workflow
"succeeded".

It fetches from the public dashboard repo (raw.githubusercontent.com —
no auth, no cloning) plus a couple of files in this engine repo, computes
each artifact's age against an ops-hours-aware freshness budget, and writes
health.json:
  - to this repo's root (committed by the workflow / orchestrator)
  - published to the dashboard repo via the GitHub contents API
    (DASHBOARD_TOKEN), reusing the fetch/PUT pattern from
    service_deck_refresh.py.

Exit code: 0 if no check is "dead", 1 if any check is "dead" — so the
Actions workflow itself goes red and GitHub emails on failure. This is
deliberate: this workflow must NEVER mask a failure with continue-on-error
or `|| echo`.

Usage:
  python health_check.py            # run all checks, publish if DASHBOARD_TOKEN set
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
import datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")

PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
ENGINE_REPO = "johnschwinghamer94-lab/sierra-ropp-hourly"
PUB_RAW = "https://raw.githubusercontent.com/" + PUB_REPO + "/main/"
ENGINE_RAW = "https://raw.githubusercontent.com/" + ENGINE_REPO + "/main/"
PUB_API = "https://api.github.com/repos/" + PUB_REPO + "/contents/"
ENGINE_API = "https://api.github.com/repos/" + ENGINE_REPO + "/contents/"

UA = {"User-Agent": "sierra-health-check"}


def now_pt():
    return dt.datetime.now(TZ)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_api_json(url, timeout=20):
    """GitHub contents API GET — needs no auth for public repos, but this
    engine repo is private, so use DASHBOARD_TOKEN if present."""
    headers = dict(UA)
    tok = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = "token " + tok
        headers["Accept"] = "application/vnd.github+json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def in_window(t, start_h, start_m, end_h, end_m):
    """True if time-of-day t (a datetime in PT) falls within [start, end)."""
    start = t.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = t.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end_h == 0 and end_m == 0:
        # midnight end — treat as end-of-day (24:00)
        end = t.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start <= t <= end


def status_for(age_min, budget_min):
    if age_min is None:
        return "dead"
    if age_min <= budget_min:
        return "ok"
    if age_min <= 2 * budget_min:
        return "warn"
    return "dead"


def make_check(key, label, ok, ageMin, expectMaxMin, lastUpdate, note):
    return {
        "key": key,
        "label": label,
        "status": ok,
        "ageMin": ageMin,
        "expectMaxMin": expectMaxMin,
        "lastUpdate": lastUpdate,
        "note": note,
    }


def outside_ops_check(key, label, note="outside ops hours"):
    return make_check(key, label, "ok", None, None, None, note)


# An artifact this old is dead no matter what time it is. Without this floor the
# "outside ops hours" branch returned a flat "ok" for ANY age, so a feed that
# died Friday afternoon read green all weekend and every night — the check was
# structurally incapable of reporting the multi-day outage it exists to catch.
HARD_DEAD_MIN = 24 * 60


def offhours_check(key, label, age_min, budget_min, last_iso):
    """Result for a check evaluated outside its ops window: 'ok' (the producer
    isn't expected to be running), unless the artifact is stale beyond the
    absolute floor, which is a real outage at any hour."""
    if age_min is not None and age_min > HARD_DEAD_MIN:
        return make_check(key, label, "dead", age_min, budget_min, last_iso,
                          f"outside ops hours, but the artifact is {round(age_min / 60)}h old "
                          f"(> {HARD_DEAD_MIN // 60}h) — this is a real outage, not an off-hours lull.")
    return make_check(key, label, "ok", age_min, budget_min, last_iso, "outside ops hours")


# ---------------------------------------------------------------------------
# Individual checks. Each returns a check dict; each must catch its own
# exceptions and degrade to a "dead" check rather than crash the run.
# ---------------------------------------------------------------------------

def check_generated_ms_feed(key, label, filename, budget_min, window, dead_note, warn_note):
    """Generic checker for *.json files with a `generatedMs` epoch-ms field,
    published in the dashboard repo, with an ops-hours window."""
    t = now_pt()
    ops = in_window(t, *window)
    try:
        data = fetch_json(PUB_RAW + filename)
        gen_ms = data.get("generatedMs")
        if gen_ms is None:
            raise ValueError("no generatedMs field")
        last = dt.datetime.fromtimestamp(gen_ms / 1000, tz=dt.timezone.utc).astimezone(TZ)
        age_min = int((t - last).total_seconds() / 60)
    except Exception as e:
        return make_check(key, label, "dead", None, budget_min, None,
                           f"could not fetch/parse {filename}: {e}. {dead_note}")

    if not ops:
        return offhours_check(key, label, age_min, budget_min, last.isoformat())

    st = status_for(age_min, budget_min)
    note = "fresh" if st == "ok" else (warn_note if st == "warn" else dead_note)
    return make_check(key, label, st, age_min, budget_min, last.isoformat(), note)


def check_livefeed():
    return check_generated_ms_feed(
        "livefeed", "SILO live feed", "livefeed.json", 6,
        (5, 30, 0, 0),
        "SILO live-call cards on the dashboard will show stale/no recent calls.",
        "SILO live feed is running behind — cards may lag.")


def check_servicefeed():
    return check_generated_ms_feed(
        "servicefeed", "Service live feed", "servicefeed.json", 6,
        (5, 30, 0, 0),
        "Service live-call cards on the dashboard will show stale/no recent calls.",
        "Service live feed is running behind — cards may lag.")


def check_hourly():
    # 20, not 40, to match the dashboard's own stale banner (HealthBanner in
    # index.html alerts when hourly.json is older than 20 min). At 40 the two
    # disagreed about what "stale" means for the same file: on 2026-08-06 the
    # capture stalled at 08:44, the banner went red at 21 min, and this check
    # still called it fresh. status_for() is ok at age <= budget, so 20 puts
    # the ok/not-ok boundary exactly where the banner's ">20" sits. The feed
    # normally commits every ~3 min, so 20 is already generous.
    return check_generated_ms_feed(
        "hourly", "ROPP hourly capture", "hourly.json", 20,
        (6, 0, 23, 0),
        "ROPP dashboard numbers (calls ran, close rate) are stale.",
        "ROPP hourly capture is running behind schedule.")


def check_servicedata():
    t = now_pt()
    ops = in_window(t, 7, 0, 23, 0)
    budget_min = 45
    key, label = "servicedata", "Service Pulse data"
    try:
        data = fetch_json(PUB_RAW + "servicedata.json")
        upd = data.get("updated")
        if not upd:
            raise ValueError("no updated field")
        last = dt.datetime.fromisoformat(upd)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        last = last.astimezone(TZ)
        age_min = int((t - last).total_seconds() / 60)
    except Exception as e:
        return make_check(key, label, "dead", None, budget_min, None,
                           f"could not fetch/parse servicedata.json: {e}. Service Pulse numbers on the dashboard are stale.")
    if not ops:
        return offhours_check(key, label, age_min, budget_min, last.isoformat())
    st = status_for(age_min, budget_min)
    note = "fresh" if st == "ok" else ("Service Pulse data running behind." if st == "warn"
                                        else "Service Pulse numbers on the dashboard are stale.")
    return make_check(key, label, st, age_min, budget_min, last.isoformat(), note)


def check_servicecalls():
    t = now_pt()
    ops = in_window(t, 7, 0, 23, 0)
    budget_min = 60
    key, label = "servicecalls", "Calls board"
    try:
        data = fetch_json(PUB_RAW + "servicecalls.json")
        gen = data.get("generated")
        if not gen:
            raise ValueError("no generated field")
        last = dt.datetime.fromisoformat(gen)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        last = last.astimezone(TZ)
        age_min = int((t - last).total_seconds() / 60)
    except Exception as e:
        return make_check(key, label, "dead", None, budget_min, None,
                           f"could not fetch/parse servicecalls.json: {e}. Calls board is stale or missing.")
    if not ops:
        return offhours_check(key, label, age_min, budget_min, last.isoformat())
    st = status_for(age_min, budget_min)
    note = "fresh" if st == "ok" else ("Calls board running behind." if st == "warn"
                                        else "Calls board is stale.")
    return make_check(key, label, st, age_min, budget_min, last.isoformat(), note)


def check_scorecards():
    return check_generated_ms_feed(
        "scorecards", "SILO live scorecards", "scorecards.json", 180,
        (7, 0, 22, 0),
        "SILO live coach scorecards on the dashboard are frozen — reps won't see new call grades.",
        "SILO live scorecards running behind.")


def check_servicecards():
    return check_generated_ms_feed(
        "servicecards", "Service live scorecards", "servicecards.json", 180,
        (7, 0, 22, 0),
        "Service live coach scorecards on the dashboard are frozen — techs won't see new call grades.",
        "Service live scorecards running behind.")


def _check_daily_plans(key, label, filename, missing_is_warn=False):
    """coaching.json / service_coaching.json — freshness measured in DAYS via a
    `date` YYYY-MM-DD field, not minutes. expect date >= yesterday(PT)."""
    t = now_pt()
    today = t.date()
    # Plans for day D are generated ~7:00-7:15 AM PT on day D+1, so between
    # midnight and the generation window the newest legitimate plan is for
    # D-2. Without this the check cried wolf every night (flagged 2026-08-05
    # 01:00 with plans that were perfectly on time).
    expected_newest = today - dt.timedelta(days=1 if t.hour >= 8 else 2)
    yesterday = expected_newest
    try:
        data = fetch_json(PUB_RAW + filename)
    except urllib.error.HTTPError as e:
        if e.code == 404 and missing_is_warn:
            return make_check(key, label, "warn", None, None, None,
                               f"{filename} not found — expected for a brand-new file in its first week; "
                               "monitor and escalate if still missing after ~1 week.")
        return make_check(key, label, "dead", None, None, None,
                           f"could not fetch {filename}: {e}. Daily coaching plans are unavailable.")
    except Exception as e:
        return make_check(key, label, "dead", None, None, None,
                           f"could not fetch/parse {filename}: {e}. Daily coaching plans are unavailable.")

    date_str = data.get("date")
    if not date_str:
        return make_check(key, label, "dead", None, None, None,
                           f"{filename} has no 'date' field. Daily coaching plans cannot be verified fresh.")
    try:
        plan_date = dt.date.fromisoformat(date_str)
    except Exception as e:
        return make_check(key, label, "dead", None, None, date_str,
                           f"could not parse date '{date_str}' in {filename}: {e}")

    age_days = (today - plan_date).days
    age_min = age_days * 1440
    if plan_date >= expected_newest:
        st = "ok"
        note = "fresh" + ("" if t.hour >= 8 else " (pre-generation window)")
    elif age_days == (2 if t.hour >= 8 else 3):
        st = "warn"
        note = "Daily coaching plan is 2 days behind — close-rate metrics may fall back to graded-call sampling."
    else:
        st = "dead"
        note = (f"Daily coaching plan is {age_days} days stale — close-rate metrics have frozen and "
                 "will fall back to graded-call sampling.")
    return make_check(key, label, st, age_min, 1440, date_str, note)


def check_coaching():
    return _check_daily_plans("coaching", "SILO daily plans", "coaching.json")


def check_service_coaching():
    return _check_daily_plans("service_coaching", "Service daily plans", "service_coaching.json",
                               missing_is_warn=True)


def check_tgl_conv():
    """THE check that would have caught the 9-day TGL outage. This repo's own
    tgl_truth/conv.json — expect < 30h old."""
    t = now_pt()
    budget_min = 30 * 60
    key, label = "tgl_conv", "ServiceTitan TGL truth (conv.json)"
    try:
        data = fetch_json(ENGINE_RAW + "tgl_truth/conv.json")
        gen = data.get("generated")
        if not gen:
            raise ValueError("no generated field")
        last = dt.datetime.fromisoformat(gen)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        last = last.astimezone(TZ)
        age_min = int((t - last).total_seconds() / 60)
    except Exception as e:
        return make_check(key, label, "dead", None, budget_min, None,
                           f"could not fetch/parse tgl_truth/conv.json: {e}. "
                           "TGL truth data is stale or unreachable — this is the exact failure mode "
                           "that caused the 9-day frozen TGL metric.")
    st = status_for(age_min, budget_min)
    note = "fresh" if st == "ok" else (
        "TGL truth conv.json running behind — check the ServiceTitan credential/pull job." if st == "warn"
        else "TGL truth conv.json is stale — TGL conversion numbers on the dashboard are frozen. "
             "This is the exact failure mode that caused the 9-day frozen TGL metric.")
    return make_check(key, label, st, age_min, budget_min, last.isoformat(), note)


def check_tgl_truth_daily():
    """List tgl_truth/ via the GitHub contents API on this repo, find newest
    YYYY-MM-DD.json, expect >= yesterday(PT)."""
    t = now_pt()
    today = t.date()
    yesterday = today - dt.timedelta(days=1)
    key, label = "tgl_truth_daily", "TGL truth daily files"
    date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
    try:
        listing = fetch_api_json(ENGINE_API + "tgl_truth")
        dates = []
        for item in listing:
            m = date_re.match(item.get("name", ""))
            if m:
                dates.append(dt.date.fromisoformat(m.group(1)))
        if not dates:
            raise ValueError("no dated json files found in tgl_truth/")
        newest = max(dates)
    except Exception as e:
        return make_check(key, label, "dead", None, 1440, None,
                           f"could not list/parse tgl_truth/: {e}. TGL truth daily files may not be building.")

    age_days = (today - newest).days
    age_min = age_days * 1440
    if newest >= yesterday:
        st = "ok"
        note = "fresh"
    elif age_days <= 2:
        st = "warn"
        note = "TGL truth daily files are running a day behind."
    else:
        st = "dead"
        note = f"Newest TGL truth daily file is {newest.isoformat()} ({age_days} days old) — the daily TGL truth build has stopped."
    return make_check(key, label, st, age_min, 1440, newest.isoformat(), note)


def check_deck_blob():
    """Fetch service.html from the dashboard repo, regex the embedded
    SERVICE_DATA blob for dateRange, expect today's or yesterday's date."""
    t = now_pt()
    today = t.date()
    yesterday = today - dt.timedelta(days=1)
    key, label = "deck_blob", "Service classic deck blob"
    try:
        html = fetch_text(PUB_RAW + "service.html")
        m = re.search(r"const SERVICE_DATA = (\{.*\});", html)
        if not m:
            return make_check(key, label, "warn", None, None, None,
                               "could not locate SERVICE_DATA blob in service.html — deck may have been restructured.")
        blob = m.group(1)
        dr_m = re.search(r'"dateRange"\s*:\s*"([^"]*)"', blob)
        if not dr_m:
            return make_check(key, label, "warn", None, None, None,
                               "SERVICE_DATA blob found but no 'dateRange' field — cannot verify deck freshness.")
        date_range = dr_m.group(1)
    except Exception as e:
        return make_check(key, label, "warn", None, None, None,
                           f"could not fetch/parse service.html: {e}")

    # Does dateRange contain today's or yesterday's month/day numerals in any
    # common format.
    def fmt_variants(d):
        return {d.isoformat(), d.strftime("%m/%d/%Y"), d.strftime("%m/%d/%y"),
                d.strftime("%m/%d").lstrip("0"), d.strftime("%b %-d") if os.name != "nt" else d.strftime("%b %d").lstrip("0"),
                d.strftime("%B %-d") if os.name != "nt" else d.strftime("%B %d").lstrip("0")}

    hit = any(v in date_range for v in fmt_variants(today)) or any(v in date_range for v in fmt_variants(yesterday))

    if hit:
        return make_check(key, label, "ok", None, None, date_range, f"dateRange='{date_range}' mentions today/yesterday")
    return make_check(key, label, "dead", None, None, date_range,
                       f"dateRange='{date_range}' does not mention today ({today.isoformat()}) or yesterday "
                       f"({yesterday.isoformat()}) — Service classic deck may be frozen.")


CHECKS = [
    check_livefeed,
    check_servicefeed,
    check_hourly,
    check_servicedata,
    check_servicecalls,
    check_scorecards,
    check_servicecards,
    check_coaching,
    check_service_coaching,
    check_tgl_conv,
    check_tgl_truth_daily,
    check_deck_blob,
]


def run_all():
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:
            results.append(make_check(fn.__name__, fn.__name__, "dead", None, None, None,
                                       f"check crashed: {e}"))
    return results


def print_table(results):
    hdr = f"{'KEY':<18}{'LABEL':<30}{'STATUS':<8}{'AGE(min)':<10}{'BUDGET(min)':<12}{'LAST UPDATE':<28}NOTE"
    print(hdr)
    print("-" * len(hdr))
    for c in results:
        age = "" if c["ageMin"] is None else str(c["ageMin"])
        budget = "" if c["expectMaxMin"] is None else str(c["expectMaxMin"])
        last = c["lastUpdate"] or ""
        print(f"{c['key']:<18}{c['label']:<30}{c['status']:<8}{age:<10}{budget:<12}{last:<28}{c['note']}")


def _gh_headers():
    return {"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
            "Accept": "application/vnd.github+json",
            "User-Agent": "sierra-health-check"}


def publish_health(payload_text):
    """Publish health.json to the dashboard repo via the GitHub contents API,
    reusing the fetch-sha-then-PUT pattern from service_deck_refresh.py."""
    path = "health.json"
    sha = None
    try:
        req = urllib.request.Request("https://api.github.com/repos/" + PUB_REPO + "/contents/" + path,
                                      headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            sha = json.loads(r.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    body = {"message": "health check " + now_pt().strftime("%Y-%m-%d %H:%M PT"),
            "branch": "main",
            "content": base64.b64encode(payload_text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request("https://api.github.com/repos/" + PUB_REPO + "/contents/" + path,
                                  data=json.dumps(body).encode(), method="PUT",
                                  headers=dict(_gh_headers(), **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read())


def main():
    results = run_all()
    counts = {"ok": 0, "warn": 0, "dead": 0}
    for c in results:
        counts[c["status"]] += 1
    worst = "dead" if counts["dead"] else ("warn" if counts["warn"] else "ok")

    gen = now_pt()
    payload = {
        "generated": gen.isoformat(),
        "generatedMs": int(gen.timestamp() * 1000),
        "worst": worst,
        "counts": counts,
        "checks": results,
    }
    payload_text = json.dumps(payload, indent=2)

    HERE = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(HERE, "health.json"), "w") as f:
        f.write(payload_text)

    print_table(results)
    print()
    print(f"worst={worst}  counts={counts}")

    publish_failed = None
    if os.environ.get("DASHBOARD_TOKEN", "").strip():
        try:
            publish_health(payload_text)
            print("Published health.json to " + PUB_REPO)
        except Exception as e:
            publish_failed = e
            # The smoke alarm must never fail quietly. A swallowed PUT leaves the
            # PREVIOUS health.json standing on the dashboard: the banner keeps
            # showing the last successful run's statuses (quite possibly all-green)
            # while this run's real findings never reach anyone, and the workflow
            # exits 0 so nobody is emailed. Surface it and go red.
            print(f"FATAL — could not publish health.json to {PUB_REPO}: {e}. "
                  "The dashboard is still serving the PREVIOUS health.json, so its health "
                  "banner is STALE and may show green while checks are dead. "
                  f"This run's own findings were worst={worst} counts={counts}.",
                  file=sys.stderr)
    else:
        print("DASHBOARD_TOKEN not set — skipping publish (wrote local health.json only).")

    sys.exit(1 if (counts["dead"] or publish_failed) else 0)


if __name__ == "__main__":
    main()
