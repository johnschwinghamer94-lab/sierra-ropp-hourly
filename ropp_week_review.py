#!/usr/bin/env python3
"""SILO/ROPP department week-review engine (Sunday-evening prelim + Monday-
morning final re-audit). Mirrors service_data.py's week_review_main() (same
prelim/final overwrite + auditNote machinery, same cloud seed/publish
plumbing) but for the SILO department, reusing UPDATE_DASHBOARD.py's
calibrated report parsing (roster, resolve(), iter_grouped(), jobkey(),
_ropp_clean_jobs()) and ropp_live.py's live Reporting-API fetchers
(fetch_report_rows -> Excel-shaped rows for an arbitrary [frm,to] window, the
same shape UPDATE_DASHBOARD's Excel-file parsing code expects).

⚠ CALIBRATED DATA RULES (see UPDATE_DASHBOARD.py / ropp_live.py docstrings —
these burned people before, do not "simplify" them away):
  - TGLs Created: ROPP_TGLs_Created.xlsx-shaped rows (report id 642925621,
    DateType 3 = Job Start/ScheduledDate). One row = one TGL. Valid row =
    JobNumber >= 6 digits (iter_grouped already filters subtotal/header rows).
    Tech = AssignedTechnicians column (resolve() picks the SILO member),
    falling back to the report's own grouping value for the rare blank row.
  - TGL revenue: ESTIMATE AC/TGLS report (id 648754648) EstimateSalesSubtotal,
    keyed by LeadGeneratedFromSourceJobNumber, JOINED client-side to the
    week's TGL job numbers (sub_by_srcjob-style dict) — NEVER the TGLs-Created
    report's own "SalesFromLeadsCreated" column (col 8), which under-counts.
    Fetched YTD-to-week-end (not just the week window) since the matching
    estimate can be scheduled outside the TGL's own week.
  - Calls Ran: Revenue_By_JobType.xlsx-shaped rows (id 379143819), Job# col 3
    (>=6 digits), ROPP-tag-cleaned via UPDATE_DASHBOARD._ropp_clean_jobs
    (same job-level tag audit the live dashboard's Close-Rate tab uses).
  - CA Close Rate = Sold (EstimateSalesSubtotal > 0) / Ran (ESTIMATE AC/TGLS
    rows whose own ScheduledDate falls in the review week) — same
    ropp_estimates()-style join UPDATE_DASHBOARD/auto_update_dashboard.py's
    build_close_rate() already uses for the live dashboard's CA Close tab.
  - Conversion = TGLs sold (subtotal > 0 in the joined revenue set) / TGLs
    created that week.
  - Roster = UPDATE_DASHBOARD.SILO (the department's current curated roster
    constant — Alex + TEAM_A + TEAM_B). Never hardcode a separate list here.

Runs in TWO environments (same file, keep servicetitan/ + private repo copy in
sync), env var SERVICEDATA_CLOUD=1 for GitHub Actions cloud mode — identical
seed/publish machinery to service_data.py, just a different target filename
(weekreview_silo.json) and no servicedata_history.json equivalent.
"""
import base64, json, os, sys, time, datetime as dt
import urllib.request, urllib.error
from collections import defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.environ.get("SERVICEDATA_CLOUD") == "1"
sys.path.insert(0, HERE)
import st_client as st                      # noqa: E402  (unused directly, but ropp_live/UPDATE_DASHBOARD import it)
import UPDATE_DASHBOARD as U                 # noqa: E402
import ropp_live as RL                       # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
U.set_year(TODAY.year)

_T0 = time.time()
def log(msg):
    print("[%6.1fs] %s" % (time.time() - _T0, msg))


# ── cloud publish target (dashboard repo, PAT = DASHBOARD_TOKEN) — identical
# machinery to service_data.py, copied verbatim so this file has no import
# dependency on that module. ─────────────────────────────────────────────────
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"


def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "ropp-week-review",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


_GH_SHAS = {}
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
        if e.code in (409, 422):
            gh_fetch(path)
            if _GH_SHAS.get(path):
                body["sha"] = _GH_SHAS[path]
            j = _gh_req(path, "PUT", body)
        else:
            raise
    _GH_SHAS[path] = j["content"]["sha"]


def cloud_bootstrap_creds():
    from pathlib import Path
    fp = Path.home() / ".servicetitan" / "sierra.json"
    if not fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(os.environ["ST_CREDS_JSON"])


_CLOUD_SEEDED = {}
def cloud_seed_files(pairs):
    for local_path, repo_name in pairs:
        txt = gh_fetch(repo_name)
        _CLOUD_SEEDED[repo_name] = txt
        if txt:
            try:
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(txt)
            except Exception:
                pass


def cloud_publish_files(pairs):
    for local_path, repo_name in pairs:
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                new_txt = f.read()
        except Exception:
            continue
        if new_txt == _CLOUD_SEEDED.get(repo_name):
            continue
        gh_put(repo_name, new_txt, "SILO week review " + dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))


# ── week-scoped report fetchers, reusing ropp_live's live Reporting-API pull
# + UPDATE_DASHBOARD's Excel-row-shaped parsing helpers ────────────────────
def _iso(d):
    return d.isoformat()


def fetch_tgls_created(frm, to):
    """[(tech, sched_date, jobnum, revenue-key-source)] for TGLs scheduled in
    [frm,to]. Mirrors UPDATE_DASHBOARD.parse_all's TGLs-Created loop exactly
    (iter_grouped jobcol=1, tech=r[3] AssignedTechnicians w/ grp fallback,
    date=r[5] ScheduledDate, bu=r[4])."""
    rows = RL.fetch_report_rows("ROPP_TGLs_Created.xlsx", _iso(frm), _iso(to))
    out = []
    for grp, r in U.iter_grouped(rows, "Assigned Technicians", 1):
        tech = U.resolve(r[3]) or U.resolve(grp)
        d = U.to_date(r[5])
        if not tech or d is None or not (frm <= d <= to):
            continue
        out.append((tech, d, U.jobkey(r[1]), r[4]))
    return out


def fetch_estimate_sub_by_srcjob(frm, to):
    """{source_job# -> summed EstimateSalesSubtotal}, YTD-to-`to` span (no
    lower bound narrower than Jan 1) — matches sub_by_srcjob()'s un-dated
    full-file join so a TGL's paired estimate is found even if scheduled
    outside the review week."""
    # FLAT rows (grouped=False for this report, see fetch_estimate_ran_sold note):
    # col4 = LeadGeneratedFromSourceJobNumber, col8 = EstimateSalesSubtotal.
    rows = RL.fetch_report_rows("ROPP_Estimate_TGLs.xlsx", _iso(U.YEAR_START), _iso(to))
    d = {}
    for r in rows[1:]:
        k = U.jobkey(r[4])
        if k and k.isdigit() and len(k) >= 6:
            d[k] = d.get(k, 0.0) + U.fnum(r[8])
    return d


def fetch_estimate_ran_sold(frm, to):
    """[(src_tech, sched_date, subtotal)] for ESTIMATE AC/TGLS rows whose own
    ScheduledDate falls in [frm,to] — the CA-Close-Rate population (Ran = all
    rows, Sold = subtotal>0), same join auto_update_dashboard.build_close_rate
    uses via UPDATE_DASHBOARD.tgl_estimates()."""
    # NOTE: unlike TGLs-Created/Revenue (grouped=True in ropp_live.CONFIG, which
    # get a leading blank column via _convert_rows), the Estimate report is
    # grouped=False -> _convert_rows returns FLAT rows (no leading blank),
    # exactly EST_HEADER-shaped: 0=JobNumber,3=LeadGeneratedFromSourceTech,
    # 5=ScheduledDate,8=EstimateSalesSubtotal (same indices UPDATE_DASHBOARD's
    # header-detected tgl_estimates()/sub_by_srcjob() resolve to).
    rows = RL.fetch_report_rows("ROPP_Estimate_TGLs.xlsx", _iso(frm), _iso(to))
    out = []
    for r in rows[1:]:
        k = U.jobkey(r[0])           # flat col0 = JobNumber
        if not (k and k.isdigit() and len(k) >= 6):
            continue
        src = U.resolve(r[3])        # flat col3 = LeadGeneratedFromSourceTech
        d = U.to_date(r[5])          # flat col5 = ScheduledDate
        if src and d is not None and frm <= d <= to:
            out.append((src, d, U.fnum(r[8])))
    return out


def fetch_calls_ran(frm, to):
    """[(tech, jobnum, bu)] Revenue_By_JobType rows in [frm,to], ROPP-tag-
    cleaned via UPDATE_DASHBOARD._ropp_clean_jobs (same clean-set the live
    dashboard's Close-Rate tab is scoped to)."""
    rows = RL.fetch_report_rows("Revenue_By_JobType.xlsx", _iso(frm), _iso(to))
    raw = []
    for grp, r in U.iter_grouped(rows, "Assigned Technicians", 3):
        tech = U.resolve(r[7]) or U.resolve(grp)
        d = U.to_date(r[4])
        if not tech or d is None or not (frm <= d <= to):
            continue
        raw.append((tech, U.jobkey(r[3]), r[10]))
    clean = U._ropp_clean_jobs({j for _, j, _ in raw})
    return [(t, j, bu) for t, j, bu in raw if j in clean]


# ── week metrics assembly ───────────────────────────────────────────────────
def week_metrics(monday, sunday):
    tgls = fetch_tgls_created(monday, sunday)
    sub_by_src = fetch_estimate_sub_by_srcjob(monday, sunday)
    ran_sold = fetch_estimate_ran_sold(monday, sunday)
    calls = fetch_calls_ran(monday, sunday)

    per = defaultdict(lambda: {"tgls": 0, "tglRevenue": 0.0, "tglsSold": 0,
                                "callsRan": 0, "estRan": 0, "estSold": 0, "caRevenue": 0.0})
    for tech, d, jobnum, bu in tgls:
        a = per[tech]
        a["tgls"] += 1
        rev = sub_by_src.get(jobnum, 0.0)
        a["tglRevenue"] += rev
        if rev > 0:
            a["tglsSold"] += 1
    for tech, jobnum, bu in calls:
        per[tech]["callsRan"] += 1
    for src, d, sub in ran_sold:
        a = per[src]
        a["estRan"] += 1
        if sub > 0:
            a["estSold"] += 1
            a["caRevenue"] += sub

    dept = {"tgls": 0, "tglRevenue": 0.0, "tglsSold": 0, "callsRan": 0,
            "estRan": 0, "estSold": 0, "caRevenue": 0.0}
    for a in per.values():
        for k in dept:
            dept[k] += a[k]

    def finish(a):
        return {
            "tgls": int(a["tgls"]),
            "tglRevenue": round(a["tglRevenue"]),
            "callsRan": int(a["callsRan"]),
            "caCloseRate": U.rate(a["estSold"], a["estRan"]),
            "conversion": U.rate(a["tglsSold"], a["tgls"]),
        }

    dept_out = finish(dept)
    techs_out = []
    for name in U.SILO:
        a = per.get(name)
        if not a or not (a["tgls"] or a["callsRan"] or a["estRan"]):
            continue
        team = "TEAM_A" if name in U.TEAM_A else ("TEAM_B" if name in U.TEAM_B else "ALEX")
        row = {"name": name, "team": team}
        row.update(finish(a))
        techs_out.append(row)
    techs_out.sort(key=lambda r: -r["tglRevenue"])
    return dept_out, techs_out


_WEEK_DELTA_KEYS = ["tgls", "tglRevenue", "callsRan", "caCloseRate", "conversion"]


def week_deltas(cur, prior):
    return {k: round(cur.get(k, 0) - prior.get(k, 0), 2) for k in _WEEK_DELTA_KEYS}


def build_audit_note(prelim_dept, final_dept):
    parts = []
    rev_delta = round(final_dept.get("tglRevenue", 0) - prelim_dept.get("tglRevenue", 0))
    if rev_delta:
        parts.append("%s$%s TGL revenue" % ("+" if rev_delta >= 0 else "-", format(abs(rev_delta), ",")))
    tgl_delta = final_dept.get("tgls", 0) - prelim_dept.get("tgls", 0)
    if tgl_delta:
        parts.append("%s%d TGL%s" % ("+" if tgl_delta >= 0 else "-", abs(tgl_delta), "" if abs(tgl_delta) == 1 else "s"))
    calls_delta = final_dept.get("callsRan", 0) - prelim_dept.get("callsRan", 0)
    if calls_delta:
        parts.append("%s%d call%s ran" % ("+" if calls_delta >= 0 else "-", abs(calls_delta), "" if abs(calls_delta) == 1 else "s"))
    if not parts:
        return "no material change from Sunday's preliminary pull"
    return " / ".join(parts) + " posted after Sunday review"


def week_review_main():
    week_arg = None
    if "--week" in sys.argv:
        week_arg = dt.date.fromisoformat(sys.argv[sys.argv.index("--week") + 1])
    log("start SILO week-review" + (" [CLOUD]" if CLOUD else ""))

    wr_path = os.path.join(HERE, "weekreview_silo.json")
    if CLOUD:
        cloud_bootstrap_creds()
        cloud_seed_files([(wr_path, "weekreview_silo.json")])
        log("cloud: seeded weekreview_silo.json from dashboard repo")

    try:
        wr = json.load(open(wr_path))
    except Exception:
        wr = {"weeks": {}, "latest": None}
    wr.setdefault("weeks", {})

    if week_arg:
        monday = week_arg - dt.timedelta(days=week_arg.weekday())
    else:
        yesterday = TODAY - dt.timedelta(days=1)
        monday = yesterday - dt.timedelta(days=yesterday.weekday())
    sunday = monday + dt.timedelta(days=6)
    key = monday.isoformat()
    label = "%d/%d-%d/%d" % (monday.month, monday.day, sunday.month, sunday.day)
    audit = "prelim" if TODAY == sunday else "final"
    log("week %s (%s): %s..%s  audit=%s" % (key, label, monday, sunday, audit))

    prior_monday = monday - dt.timedelta(weeks=1)
    prior_sunday = prior_monday + dt.timedelta(days=6)

    log("SILO roster: %d techs" % len(U.SILO))

    log("current week metrics")
    cur_dept, cur_techs = week_metrics(monday, sunday)
    log("prior week metrics")
    prior_dept, _ = week_metrics(prior_monday, prior_sunday)

    cur_dept["deltas"] = week_deltas(cur_dept, prior_dept)

    prior_entry = wr["weeks"].get(key)
    if audit == "final" and prior_entry and prior_entry.get("audit") == "prelim":
        auditNote = build_audit_note(prior_entry["dept"], cur_dept)
    elif audit == "final" and week_arg and not prior_entry:
        auditNote = "generated retroactively"
    elif audit == "final":
        auditNote = "finalized (no prior prelim run)"
    else:
        auditNote = "preliminary — Sunday evening pull; will re-audit Monday morning"

    wr["weeks"][key] = {"label": label, "generated": NOW.isoformat(), "audit": audit,
                         "dept": cur_dept, "techs": cur_techs, "auditNote": auditNote}
    wr["latest"] = key

    with open(wr_path + ".tmp", "w") as f:
        json.dump(wr, f, separators=(",", ":"))
    os.replace(wr_path + ".tmp", wr_path)
    log("wrote %s (week %s, audit=%s)" % (wr_path, key, audit))

    if CLOUD:
        cloud_publish_files([(wr_path, "weekreview_silo.json")])
        log("cloud: published weekreview_silo.json to dashboard repo")

    log("DONE in %.1fs" % (time.time() - _T0))
    print("\nWeek %s (%s) audit=%s — %s" % (key, label, audit, auditNote))
    print("dept: TGLs %d | TGL revenue $%s | calls ran %d | CA close %.1f%% | conversion %.1f%%" % (
        cur_dept["tgls"], format(cur_dept["tglRevenue"], ","), cur_dept["callsRan"],
        cur_dept["caCloseRate"], cur_dept["conversion"]))
    print("deltas vs prior week:", cur_dept["deltas"])
    print("techs: %d rows; top 3:" % len(cur_techs),
          [(t["name"], t["tglRevenue"], t["conversion"]) for t in cur_techs[:3]])


if __name__ == "__main__":
    if "--week-review" in sys.argv:
        week_review_main()
    else:
        print("usage: ropp_week_review.py --week-review [--week YYYY-MM-DD]")
        sys.exit(1)
