#!/usr/bin/env python3
"""HVAC-Service department data engine for the new Service dashboard.

Dept = 25 active techs on dispatch teams "2a Service NO Sam Maintenance" +
"2b Service with SAM Maintenance" (fetched dynamically from
settings/v2/technicians — never hardcoded). Dept business units: HVAC -
Service (333) + HVAC - Maintenance (342817560).

Data sources (see REPORT BACK for calibration notes):
  - opps / close rate / leads set / options-per-opp / membership conversion
    <- Reporting API, Field Conversion Report v1 (technician/328361546), the
    same report id ropp/service_live.py already calibrates against. Fetched
    once per window (today/wtd/mtd/ytd) and once per elapsed month.
  - sales (sold-estimate $) <- sales/v2/estimates (soldAfter=Jan 1), joined
    job -> firstAppointmentId -> dispatch/v2/appointment-assignments -> roster
    tech. Pulled ONCE for the full YTD span and bucketed client-side into all
    four windows (avoids 4x redundant entity pulls).
  - revenue (invoiced $, BU split) <- accounting/v2/invoices (invoicedOnOrAfter
    =Jan 1), filtered to the two dept BUs, subTotal, bucketed the same way.
  - board (today only) <- jpm/v2/appointments for today + jpm/v2/jobs for BU.

Writes servicedata.json + appends a per-day snapshot to
servicedata_history.json (deduped on date).

Runs in TWO environments (same file, keep private repo & servicetitan/ in
sync):
  Windows (default, no env): reads/writes servicedata.json + history LOCALLY
    only (servicetitan/ folder), FC report day/week caches go to the local
    scratchpad dir. No git, no cloud push.
  Cloud (SERVICEDATA_CLOUD=1, GitHub Actions): before running, SEEDS
    servicedata.json + servicedata_history.json from the dashboard repo
    (contents API, DASHBOARD_TOKEN) into the working dir so --light mode's
    cache-reuse (past techDaily days, past weekly weeks, mtd/ytd passthrough)
    works on a fresh runner; a 404 (first run) falls back to full-run
    behavior. After the run, PUBLISHES both files back to the dashboard repo,
    skip-if-unchanged. Creds come from ST_CREDS_JSON (materializes
    ~/.servicetitan/sierra.json). FC report day/week caches (fc_day_*.json /
    fc_week_*.json) are pointless across fresh cloud runners — light runs
    don't need them (they reuse straight from the seeded servicedata.json),
    and full runs (once/day) simply refetch, which is an acceptable cost.
    Ported from servicefeed_sync.py's proven cloud machinery.
"""
import base64, json, os, sys, time, datetime as dt
import urllib.request, urllib.error
from collections import defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.environ.get("SERVICEDATA_CLOUD") == "1"
# FC report day/week caches: configurable via env, default to the local
# scratchpad dir on Windows. In cloud mode there's no point caching across a
# fresh runner, so point it at a throwaway per-run temp dir instead.
if os.environ.get("SERVICEDATA_CACHE_DIR"):
    SCRATCH = os.environ["SERVICEDATA_CACHE_DIR"]
elif CLOUD:
    import tempfile
    SCRATCH = os.path.join(tempfile.gettempdir(), "servicedata_fc_cache")
else:
    SCRATCH = r"C:\Users\johns\AppData\Local\Temp\claude\C--Users-johns-OneDrive---Sierra-Cools-LV-CLAUDE-STUFF\7aff2463-7eed-48f8-baaa-dccbf30c2f3e\scratchpad"
os.makedirs(SCRATCH, exist_ok=True)
sys.path.insert(0, HERE)
import st_client as st

# ── cloud publish target (dashboard repo, PAT = DASHBOARD_TOKEN) ───────────
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"


def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "service-data",
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
        if e.code in (409, 422):          # sha raced (another writer) — refresh and retry once
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
    """Pull each (local_path, repo_name) pair from the dashboard repo into the
    working dir so --light mode's cache reuse works on a fresh runner. 404
    (first run) is fine — caller falls back to a full run."""
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
    """Push each (local_path, repo_name) pair back to the dashboard repo,
    skip-if-unchanged vs. what cloud_seed_files() pulled at session start."""
    for local_path, repo_name in pairs:
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                new_txt = f.read()
        except Exception:
            continue
        if new_txt == _CLOUD_SEEDED.get(repo_name):
            continue
        gh_put(repo_name, new_txt, "Service data " + dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"))


def cloud_seed(path_data, path_hist):
    cloud_seed_files([(path_data, "servicedata.json"), (path_hist, "servicedata_history.json")])


def cloud_publish(path_data, path_hist):
    cloud_publish_files([(path_data, "servicedata.json"), (path_hist, "servicedata_history.json")])

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
YEAR = TODAY.year

BU_SERVICE = 333
BU_MAINT = 342817560
DEPT_BUS = {BU_SERVICE, BU_MAINT}
BU_NAME = {BU_SERVICE: "HVAC - Service", BU_MAINT: "HVAC - Maintenance"}
TEAMS = {"2a Service NO Sam Maintenance": "2A", "2b Service with SAM Maintenance": "2B"}
FC_REPORT = ("technician", 328361546)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

_T0 = time.time()
def log(msg):
    print("[%6.1fs] %s" % (time.time() - _T0, msg))


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def utc_iso(d, end_of_day=False):
    """Local (Vegas) midnight of date d -> UTC 'Z' timestamp string."""
    t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
    local = dt.datetime.combine(d, t, tzinfo=TZ)
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    try:
        base = s.replace("Z", "").split(".")[0]
        return dt.datetime.fromisoformat(base).replace(tzinfo=dt.timezone.utc).astimezone(TZ)
    except Exception:
        return None


def parse_date_only(s):
    """ServiceTitan invoiceDate is a pure calendar date stamped at UTC midnight
    (e.g. '2026-07-29T00:00:00Z' means the calendar day 2026-07-29, not
    '2026-07-28' local) — do NOT run it through a UTC->Pacific conversion or
    every date shifts back a day. Take the date portion literally."""
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        return None


# ── window boundaries ────────────────────────────────────────────────────────
WTD_START = TODAY - dt.timedelta(days=TODAY.weekday())      # Monday
MTD_START = TODAY.replace(day=1)
YTD_START = dt.date(YEAR, 1, 1)
WINDOWS = {"today": (TODAY, TODAY), "wtd": (WTD_START, TODAY),
           "mtd": (MTD_START, TODAY), "ytd": (YTD_START, TODAY)}


# ── paged entity fetch (with 429/5xx retry already inside st_client) ───────
def paged(path, params, page_size=500):
    out, page = [], 1
    while True:
        r = st.api_get(path, dict(params, page=page, pageSize=page_size))
        out += r.get("data", [])
        if not r.get("hasMore"):
            return out
        page += 1
        time.sleep(0.15)


def chunked_get(path, ids, extra=None, key="ids", size=50):
    out, ids = [], list(ids)
    for i in range(0, len(ids), size):
        params = dict(extra or {})
        params[key] = ",".join(str(x) for x in ids[i:i + size])
        out += paged(path, params)
        time.sleep(0.15)
    return out


def run_report_all(cat, rid, parameters):
    out, page, fields = [], 1, None
    while True:
        r = st.run_report(cat, rid, parameters, page=page, page_size=5000)
        fields = [f["name"] for f in r.get("fields", [])]
        out += r.get("data", [])
        if not r.get("hasMore") or not r.get("data"):
            break
        page += 1
        time.sleep(0.5)
    return fields, out


# ── 1. roster ────────────────────────────────────────────────────────────────
def fetch_roster():
    techs = paged("/settings/v2/tenant/{tenant}/technicians", {"active": "true"}, page_size=200)
    roster = {}
    for t in techs:
        team = t.get("team")
        if team in TEAMS:
            roster[t["name"].strip()] = {"team": TEAMS[team], "id": t["id"]}
    return roster


# ── 2. Field Conversion v1 report, per window + per month ──────────────────
def fc_window(frm, to):
    params = [{"name": "From", "value": frm.isoformat()}, {"name": "To", "value": to.isoformat()},
              {"name": "IncludeInactive", "value": False}]
    fields, rows = run_report_all(*FC_REPORT, params)
    c = {n: i for i, n in enumerate(fields)}
    return c, rows


def fc_tech_rows(c, rows, roster):
    """dept-BU rows for roster techs, summed per tech (a tech can have a
    Service row and a Maintenance row — both count toward the department)."""
    agg = {}
    for r in rows:
        name = str(r[c["Name"]]).strip()
        bu = str(r[c["TechnicianBusinessUnit"]] or "").strip()
        if name not in roster or bu not in BU_NAME.values():
            continue
        a = agg.setdefault(name, {"jobs": 0, "opp": 0.0, "converted": 0.0, "rev": 0.0,
                                    "leads": 0, "membSold": 0.0, "membOpp": 0.0, "oppW": 0.0})
        opp = num(r[c["Opportunity"]]); conv = num(r[c["OpportunityConversionRate"]])
        rev = num(r[c["CompletedRevenueWithAdjustments"]])
        membConv = num(r[c["MembershipConversionRate"]])
        opo = num(r[c["OptionsPerOpportunity"]])
        a["jobs"] += num(r[c["CompletedJobs"]])
        a["opp"] += opp
        a["converted"] += round(opp * conv)
        a["rev"] += rev
        a["leads"] += int(num(r[c["LeadsSet"]]))
        a["membSold"] += round(opp * membConv)
        a["membOpp"] += opp
        a["oppW"] += opo * opp
    return agg


# ── 3. sold estimates (sales$) — entity precision, but ONLY for Today/WTD.
# A company-wide sold-estimates pull back to Jan 1 (all departments, not just
# the 25 roster techs — the API has no server-side technician filter on this
# endpoint or on appointment-assignments) proved too heavy (unbounded job/tech
# join). Per the task's budget guidance, MTD/YTD sales instead use the
# calibrated Field Conversion v1 report's CompletedRevenueWithAdjustments
# figure (see fc_tech_rows "rev") — labeled "salesApprox" fallback in the
# per-tech rows / dept summary for those two windows. Today/WTD stay entity-
# precise since the estimate volume in a ≤7-day window is small enough to
# join to jobs/assignments quickly.
def fetch_sold_estimates_recent():
    """BUG FIX (2026-07-30): the raw pull is company-wide (every BU, every
    department) since the API has no server-side technician OR businessUnit
    filter on this endpoint. bucket_sales() used to sum every row's subtotal
    into the dept total regardless of businessUnitId or tech attribution —
    on the week this was caught, of 326 rows only 244 were even on a dept BU
    (333/342817560); the other 82 were BU 370/353/354/595105985/340802904
    (Install and other non-Service-dept business units) worth ~$964k, e.g. a
    single $55,160 estimate (id 669275198, BU 370, soldOn 2026-07-28) that
    has nothing to do with the 25-tech HVAC-Service roster. That inflated
    wtd.sales to $1.07M against a techDaily/per-tech sum of ~$105k. Filtering
    to dept BUs here (server-can't, so client-side) keeps every downstream
    consumer (bucket_sales, bucket_sales_by_day/techDaily) working off the
    same correct, dept-scoped estimate set."""
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": utc_iso(WTD_START)})
    ests = [e for e in ests if e.get("soldOn") and e.get("businessUnitId") in DEPT_BUS]
    log("sold estimates WTD-to-date (dept BUs only): %d rows" % len(ests))
    return ests


def fetch_job_tech_map(job_ids, roster):
    """job id -> roster tech name, via job.firstAppointmentId -> appointment-assignments."""
    job_ids = sorted(set(job_ids))
    jobs = chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)
    appt_ids = [j["firstAppointmentId"] for j in jobs if j.get("firstAppointmentId")]
    appt_to_job = {j["firstAppointmentId"]: j["id"] for j in jobs if j.get("firstAppointmentId")}
    asg = chunked_get("/dispatch/v2/tenant/{tenant}/appointment-assignments", appt_ids, key="appointmentIds")
    job_tech = {}
    for a in asg:
        if not a.get("active"):
            continue
        name = a.get("technicianName")
        if name not in roster:
            continue
        jid = a.get("jobId") or appt_to_job.get(a.get("appointmentId"))
        if jid and jid not in job_tech:
            job_tech[jid] = name
    return job_tech


ENTITY_SALES_WINDOWS = ("today", "wtd")   # see fetch_sold_estimates_recent() note


def bucket_sales(ests, job_tech):
    """{window: {tech: {"sales": $, "n": count}}} — today/wtd only (entity-precise).
    BUG FIX (2026-07-30): dept[w] now only accumulates rows that resolved to a
    roster tech (same condition as the per-tech buckets and as techDaily's
    bucket_sales_by_day) so wtd.sales / weekly[current].sales reconcile
    EXACTLY with the sum of techs.wtd[].sales and techDaily weekTotals —
    previously it summed every dept-BU estimate even when job->tech
    attribution failed, silently double-counting against no tech."""
    out = {w: defaultdict(lambda: {"sales": 0.0, "n": 0}) for w in ENTITY_SALES_WINDOWS}
    dept = {w: 0.0 for w in ENTITY_SALES_WINDOWS}
    for e in ests:
        d = parse_ts(e.get("soldOn"))
        if not d:
            continue
        d = d.date()
        tech = job_tech.get(e.get("jobId"))
        if not tech:
            continue
        sub = num(e.get("subtotal"))
        for w in ENTITY_SALES_WINDOWS:
            frm, to = WINDOWS[w]
            if frm <= d <= to:
                dept[w] += sub
                out[w][tech]["sales"] += sub
                out[w][tech]["n"] += 1
    return out, dept


# ── 4. invoices (revenue$, BU split) — YTD span, bucketed ──────────────────
def fetch_invoices():
    """Server-side businessUnitId filter (confirmed to work) keeps this to just
    the two dept BUs instead of a company-wide invoice pull."""
    invs = []
    for bu in DEPT_BUS:
        invs += paged("/accounting/v2/tenant/{tenant}/invoices",
                       {"businessUnitId": bu, "invoicedOnOrAfter": utc_iso(YTD_START)})
        time.sleep(0.3)
    log("dept invoices YTD: %d rows" % len(invs))
    return invs


def bucket_revenue(invs, windows=None):
    """{window: {"total":$, "bu":{333:$,342817560:$}}}"""
    windows = windows or WINDOWS
    out = {w: {"total": 0.0, "bu": {BU_SERVICE: 0.0, BU_MAINT: 0.0}} for w in windows}
    for i in invs:
        d = parse_date_only(i.get("invoiceDate"))
        if not d:
            continue
        sub = num(i.get("subTotal"))
        buid = (i.get("businessUnit") or {}).get("id")
        for w, (frm, to) in windows.items():
            if frm <= d <= to:
                out[w]["total"] += sub
                if buid in out[w]["bu"]:
                    out[w]["bu"][buid] += sub
    return out


def revenue_for_range(invs, frm, to):
    tot = 0.0
    for i in invs:
        d = parse_date_only(i.get("invoiceDate"))
        if d and frm <= d <= to:
            tot += num(i.get("subTotal"))
    return tot


# ── 5. monthly series Jan..current month ────────────────────────────────────
# "sales" here is the FC v1 report's CompletedRevenueWithAdjustments sum
# (same fallback used for MTD/YTD tech rows — see fetch_sold_estimates_recent
# note) since a per-month entity sold-estimate pull x7 months company-wide
# would blow the runtime budget. "revenue" (BU split) is entity-precise from
# the invoices pull, which already covers the full YTD span.
def monthly_series(invs, roster):
    import calendar
    out = []
    for m in range(1, TODAY.month + 1):
        m_from = dt.date(YEAR, m, 1)
        last = calendar.monthrange(YEAR, m)[1]
        m_to = TODAY if m == TODAY.month else dt.date(YEAR, m, last)
        c, rows = fc_window(m_from, m_to)
        agg = fc_tech_rows(c, rows, roster)
        opp = sum(a["opp"] for a in agg.values())
        conv = sum(a["converted"] for a in agg.values())
        sales_approx = sum(a["rev"] for a in agg.values())
        membOpp = sum(a["membOpp"] for a in agg.values())
        membSold = sum(a["membSold"] for a in agg.values())
        rev_bu = {BU_SERVICE: 0.0, BU_MAINT: 0.0}
        for i in invs:
            d = parse_date_only(i.get("invoiceDate"))
            if d and m_from <= d <= m_to:
                buid = (i.get("businessUnit") or {}).get("id")
                if buid in rev_bu:
                    rev_bu[buid] += num(i.get("subTotal"))
        out.append({"month": MONTHS[m - 1], "salesApprox": round(sales_approx),
                     "revenue": round(sum(rev_bu.values())), "revenueBU": {BU_NAME[k]: round(v) for k, v in rev_bu.items()},
                     "opp": int(opp), "converted": int(conv),
                     "closeRate": round(conv / opp * 1000) / 10 if opp else 0,
                     "membershipsSold": int(membSold),
                     "membershipConv": round(membSold / membOpp * 1000) / 10 if membOpp else 0})
        log("monthly %s done" % MONTHS[m - 1])
        time.sleep(0.5)
    return out


# ── 6. board counts (today) ─────────────────────────────────────────────────
def board_counts():
    day0 = dt.datetime.combine(TODAY, dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    day1 = day0 + dt.timedelta(days=1)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    appts = paged("/jpm/v2/tenant/{tenant}/appointments",
                  {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
    appts = [a for a in appts if a.get("status") != "Canceled"]
    job_ids = sorted({a["jobId"] for a in appts if a.get("jobId")})
    jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)}
    board = {"demandOnBoard": 0, "demandRan": 0, "maintenanceOnBoard": 0, "maintenanceRan": 0}
    for a in appts:
        j = jobs.get(a.get("jobId"), {})
        bu = j.get("businessUnitId")
        ran = (a.get("status") == "Done") or (j.get("jobStatus") == "Completed")
        if bu == BU_SERVICE:
            board["demandOnBoard"] += 1
            board["demandRan"] += int(ran)
        elif bu == BU_MAINT:
            board["maintenanceOnBoard"] += 1
            board["maintenanceRan"] += int(ran)
    return board


# ── 7. per-tech DAY-BY-DAY detail, current week (WTD_START..TODAY) ─────────
# opps/close/mem <- one FC v1 report call PER elapsed day (past days cached to
# scratchpad forever — a completed day's numbers never change; today is always
# refetched fresh). sales$ <- the SAME entity-precise sold-estimate pull /
# job_tech map already fetched for the WTD window (fetch_sold_estimates_recent /
# fetch_job_tech_map) — no extra API calls, just bucketed per calendar day
# instead of summed over the whole window.
def _fc_day_cache_path(d):
    return os.path.join(SCRATCH, "fc_day_%s.json" % d.isoformat())


def fetch_fc_day_agg(d, roster):
    """Per-tech FC agg (see fc_tech_rows) for a single calendar day, cached to
    scratchpad for past days (immutable); today is always fetched live."""
    cache = _fc_day_cache_path(d)
    if d < TODAY and os.path.exists(cache):
        try:
            payload = json.load(open(cache))
            return payload
        except Exception:
            pass
    c, rows = fc_window(d, d)
    agg = fc_tech_rows(c, rows, roster)
    if d < TODAY:
        try:
            with open(cache + ".tmp", "w") as f:
                json.dump(agg, f)
            os.replace(cache + ".tmp", cache)
        except Exception:
            pass
    return agg


def bucket_sales_by_day(ests, job_tech, days):
    """{day_iso: {tech: sales$}} from the already-fetched WTD entity estimates."""
    out = {d.isoformat(): defaultdict(float) for d in days}
    dayset = set(days)
    for e in ests:
        ts = parse_ts(e.get("soldOn"))
        if not ts:
            continue
        d = ts.date()
        if d not in dayset:
            continue
        tech = job_tech.get(e.get("jobId"))
        if tech:
            out[d.isoformat()][tech] += num(e.get("subtotal"))
    return out


def build_tech_daily(roster, ests, job_tech, old_tech_daily=None, light=False):
    """techDaily = {techName: {'YYYY-MM-DD': {...}, ..., 'weekTotal': {...}}, '_days': [...]}."""
    days = [WTD_START + dt.timedelta(days=i) for i in range((TODAY - WTD_START).days + 1)]
    sales_by_day = bucket_sales_by_day(ests, job_tech, days)

    day_aggs = {}
    for d in days:
        if light and d < TODAY and old_tech_daily:
            day_aggs[d] = None   # signal: reuse from old_tech_daily below
        else:
            day_aggs[d] = fetch_fc_day_agg(d, roster)
        log("  techDaily %s done" % d.isoformat())

    out = {"_days": [d.isoformat() for d in days]}
    for name in roster:
        rec = {}
        tot = {"sales": 0.0, "opps": 0, "converted": 0, "membershipsSold": 0}
        for d in days:
            diso = d.isoformat()
            if day_aggs[d] is None:
                # light mode, past day: reuse verbatim from previous run's file
                old = (old_tech_daily or {}).get(name, {}).get(diso)
                cell = old or {"sales": 0, "opps": 0, "closeRate": 0, "membershipsSold": 0}
            else:
                a = day_aggs[d].get(name)
                opp = a["opp"] if a else 0
                conv = a["converted"] if a else 0
                mem = a["membSold"] if a else 0
                sales = sales_by_day.get(diso, {}).get(name, 0.0)
                cell = {"sales": round(sales), "opps": int(opp),
                        "closeRate": round(conv / opp * 1000) / 10 if opp else 0,
                        "membershipsSold": int(mem)}
            rec[diso] = cell
            tot["sales"] += cell["sales"]
            tot["opps"] += cell["opps"]
            tot["membershipsSold"] += cell["membershipsSold"]
            # reconstruct converted-count for accurate week close rate
            tot["converted"] += round(cell["opps"] * cell["closeRate"] / 100) if cell["opps"] else 0
        rec["weekTotal"] = {
            "sales": round(tot["sales"]), "opps": tot["opps"],
            "closeRate": round(tot["converted"] / tot["opps"] * 1000) / 10 if tot["opps"] else 0,
            "membershipsSold": tot["membershipsSold"],
        }
        if tot["sales"] or tot["opps"] or tot["membershipsSold"]:
            out[name] = rec
    return out


# ── 8. weekly dept series, last 9 weeks (W1..W8 past + current WTD) ────────
# revenue <- bucketed from the already-fetched invoices pull (free). close
# rate/sales(approx) <- one FC v1 report call PER past week, cached to
# scratchpad forever (a completed week never changes); the current week reuses
# the FC "wtd" window + entity sold-estimate dept total already computed in
# main() — no extra calls.
def _weekly_cache_path(monday):
    return os.path.join(SCRATCH, "fc_week_%s.json" % monday.isoformat())


# Weeks currently tracked by weekreview.json with audit=="prelim" must NEVER
# be read from / written to the fc_week_*.json cache — a Sunday-evening
# prelim pull is known-incomplete (posting batches land overnight) and the
# Monday final re-audit needs a genuinely fresh fetch. Populated once at the
# top of week_review_main(); stays {} for normal (non-week-review) runs, so
# the historical 9-week series keeps its old always-cache behavior.
_WR_AUDIT = {}


def _week_cache_allowed(monday):
    return _WR_AUDIT.get(monday.isoformat()) != "prelim"


def fc_week_agg(monday, sunday, roster):
    """Per-tech FC agg (fc_tech_rows shape) for an arbitrary Mon-Sun week,
    cached to scratchpad forever UNLESS that week is currently tracked in
    weekreview.json with audit=='prelim' (see _WR_AUDIT note above)."""
    cache = _weekly_cache_path(monday)
    allow_cache = _week_cache_allowed(monday)
    if allow_cache and os.path.exists(cache):
        try:
            payload = json.load(open(cache))
            if isinstance(payload, dict) and "_tech_agg" in payload:
                return payload["_tech_agg"]
        except Exception:
            pass
    c, rows = fc_window(monday, sunday)
    agg = fc_tech_rows(c, rows, roster)
    if allow_cache:
        try:
            with open(cache + ".tmp", "w") as f:
                json.dump({"_tech_agg": agg}, f)
            os.replace(cache + ".tmp", cache)
        except Exception:
            pass
    return agg


def fetch_fc_week_dept(monday, sunday, roster):
    agg = fc_week_agg(monday, sunday, roster)
    dept = {"opp": sum(a["opp"] for a in agg.values()),
            "converted": sum(a["converted"] for a in agg.values()),
            "rev": sum(a["rev"] for a in agg.values()),
            "membOpp": sum(a["membOpp"] for a in agg.values()),
            "membSold": sum(a["membSold"] for a in agg.values())}
    return dept


def weekly_series(roster, invs_for_revenue, current_wtd_fc_agg, current_wtd_sales_dept,
                   old_weekly=None, light=False):
    weeks = []
    for n_back in range(8, -1, -1):
        monday = WTD_START - dt.timedelta(weeks=n_back)
        sunday = monday + dt.timedelta(days=6)
        is_current = (n_back == 0)
        label = "%d/%d-%d/%d" % (monday.month, monday.day, sunday.month, sunday.day)
        if is_current:
            label += " (WTD)"
            opp = sum(a["opp"] for a in current_wtd_fc_agg.values())
            converted = sum(a["converted"] for a in current_wtd_fc_agg.values())
            membOpp = sum(a["membOpp"] for a in current_wtd_fc_agg.values())
            membSold = sum(a["membSold"] for a in current_wtd_fc_agg.values())
            sales = current_wtd_sales_dept
        elif light and old_weekly and len(old_weekly) == 9:
            weeks.append(old_weekly[8 - n_back])
            continue
        else:
            dept = fetch_fc_week_dept(monday, sunday, roster)
            opp, converted = dept["opp"], dept["converted"]
            membOpp, membSold = dept["membOpp"], dept["membSold"]
            sales = dept["rev"]
        revenue = revenue_for_range(invs_for_revenue, monday, sunday)
        weeks.append({
            "label": label, "start": monday.isoformat(), "end": sunday.isoformat(),
            "sales": round(sales), "salesIsApprox": not is_current,
            "revenue": round(revenue),
            "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
            "membershipConv": round(membSold / membOpp * 1000) / 10 if membOpp else 0,
        })
    return weeks


# ── 8b. forward-looking calls board (today .. +7 days, first 5 non-Sunday) ──
# Appointment/job/BU resolution mirrors board_counts(); tech resolution reuses
# the SAME dispatch/v2/appointment-assignments-by-appointmentIds workaround as
# fetch_job_tech_map() (that endpoint ignores date filters server-side, so we
# always fetch appointments by date range first, then query assignments in
# appointmentIds= chunks). Runtime stays modest: one appointments call + one
# job-types/business-units-scale job/customer chunked_get + one assignments
# chunked_get, all for an 8-day window.
CALLS_WINDOW_DAYS = 8   # today .. +7 inclusive
_MAINT_KEYWORDS = ("maintenance", "tune-up", "tune up", "sam")

_JOB_TYPES_CACHE = {}
def fetch_job_types():
    if "date" not in _JOB_TYPES_CACHE or _JOB_TYPES_CACHE["date"] != TODAY.isoformat():
        jts = paged("/jpm/v2/tenant/{tenant}/job-types", {})
        _JOB_TYPES_CACHE["map"] = {t["id"]: t.get("name", "") for t in jts}
        _JOB_TYPES_CACHE["date"] = TODAY.isoformat()
    return _JOB_TYPES_CACHE["map"]


def _fmt_appt_time(t):
    if not t:
        return ""
    s = t.strftime("%I:%M%p").lstrip("0")
    return s[:-2] + ("p" if s.endswith("PM") else "a")


def build_calls_board(roster):
    """Forward calls board: {generated, generatedMs, days:[{date,label,total,
    demand,maintenance,unassigned,techs:[{name,team,appts:[...]}]}]} for the
    first 5 non-Sunday calendar days starting today."""
    start = TODAY
    end = TODAY + dt.timedelta(days=CALLS_WINDOW_DAYS - 1)
    day0 = dt.datetime.combine(start, dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    day1 = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    appts = paged("/jpm/v2/tenant/{tenant}/appointments",
                  {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
    appts = [a for a in appts if a.get("status") != "Canceled"]
    job_ids = sorted({a["jobId"] for a in appts if a.get("jobId")})
    jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)}
    dept_appts = [a for a in appts if jobs.get(a.get("jobId"), {}).get("businessUnitId") in DEPT_BUS]

    cust_ids = sorted({jobs[a["jobId"]].get("customerId") for a in dept_appts
                        if jobs.get(a["jobId"], {}).get("customerId")})
    custs = {c["id"]: c.get("name", "") for c in chunked_get("/crm/v2/tenant/{tenant}/customers", cust_ids)}
    jts = fetch_job_types()

    appt_ids = [a["id"] for a in dept_appts]
    asg = chunked_get("/dispatch/v2/tenant/{tenant}/appointment-assignments", appt_ids, key="appointmentIds")
    appt_techs = defaultdict(list)
    for a in asg:
        if a.get("active"):
            nm = a.get("technicianName")
            if nm:
                appt_techs[a.get("appointmentId")].append(nm)

    days_out = []
    d = start
    while d <= end and len(days_out) < 5:
        if d.weekday() != 6:      # skip Sunday
            days_out.append(d)
        d += dt.timedelta(days=1)

    dayset = set(days_out)
    by_day = {d.isoformat(): [] for d in days_out}

    for a in dept_appts:
        start_l = parse_ts(a.get("start"))
        if not start_l or start_l.date() not in dayset:
            continue
        j = jobs.get(a.get("jobId"), {})
        bu = j.get("businessUnitId")
        jt_name = jts.get(j.get("jobTypeId"), "") or ""
        is_maint = bu == BU_MAINT or any(k in jt_name.lower() for k in _MAINT_KEYWORDS)
        names = appt_techs.get(a["id"], [])
        roster_names = [n for n in names if n in roster]
        if roster_names:
            tech_name, team = roster_names[0], roster[roster_names[0]]["team"]
        elif names:
            tech_name, team = names[0], ""
        else:
            tech_name, team = "UNASSIGNED", ""
        cust_name = custs.get(j.get("customerId"), "") or "—"
        by_day[start_l.date().isoformat()].append({
            "time": _fmt_appt_time(start_l), "customer": cust_name, "jobType": jt_name,
            "jobId": j.get("id") or a.get("jobId"),
            "kind": "maintenance" if is_maint else "demand",
            "_tech": tech_name, "_team": team, "_startIso": a.get("start") or "9999",
        })

    out_days = []
    for d in days_out:
        diso = d.isoformat()
        entries = sorted(by_day[diso], key=lambda x: x["_startIso"])
        techs_map = {}
        for e in entries:
            grp = techs_map.setdefault(e["_tech"], {"name": e["_tech"], "team": e["_team"], "appts": []})
            grp["appts"].append({"time": e["time"], "customer": e["customer"], "jobType": e["jobType"],
                                  "jobId": e["jobId"], "kind": e["kind"]})
        unassigned_grp = techs_map.pop("UNASSIGNED", None)
        tech_list = sorted(techs_map.values(), key=lambda g: -len(g["appts"]))
        if unassigned_grp:
            tech_list.append(unassigned_grp)
        total = len(entries)
        maint = sum(1 for e in entries if e["kind"] == "maintenance")
        unassigned_n = len(unassigned_grp["appts"]) if unassigned_grp else 0
        out_days.append({
            "date": diso, "label": d.strftime("%a").upper() + " %d/%d" % (d.month, d.day),
            "total": total, "demand": total - maint, "maintenance": maint, "unassigned": unassigned_n,
            "techs": tech_list,
        })
    return {"generated": NOW.isoformat(), "generatedMs": int(NOW.timestamp() * 1000), "days": out_days}


def update_calls_history(days, hist_path):
    """Append at most one snapshot per clock hour, keep newest 400."""
    try:
        hist = json.load(open(hist_path))
        if not isinstance(hist, list):
            hist = []
    except Exception:
        hist = []
    now_hour = NOW.strftime("%Y-%m-%dT%H")
    if not hist or not str(hist[-1].get("ts", "")).startswith(now_hour):
        counts = {d["date"]: d["total"] for d in days}
        hist.append({"ts": NOW.isoformat(), "counts": counts})
        hist = hist[-400:]
        with open(hist_path + ".tmp", "w") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(hist_path + ".tmp", hist_path)
    return hist


# ── 9. budget block ──────────────────────────────────────────────────────────
def budget_block(invs_for_month):
    path = os.path.join(HERE, "service_budget.json")
    try:
        cfg = json.load(open(path))
    except Exception:
        return None
    import calendar
    y, m = (int(x) for x in cfg["month"].split("-"))
    month_start = dt.date(y, m, 1)
    last_day = calendar.monthrange(y, m)[1]
    month_end = dt.date(y, m, last_day)
    out = {"month": cfg["month"], "revenueTarget": cfg["revenueTarget"],
           "dailyRevTarget": cfg["dailyRevTarget"], "workingDays": cfg["workingDays"],
           "avgTicketTarget": cfg["avgTicketTarget"]}
    if TODAY < month_start:
        out["todayCommit"] = cfg["dailyRevTarget"]
        out["preMonth"] = True
        return out
    month_actual = revenue_for_range(invs_for_month, month_start, min(TODAY, month_end))
    remaining = 0
    d = TODAY
    while d <= month_end:
        if d.weekday() < 5:
            remaining += 1
        d += dt.timedelta(days=1)
    out["monthActualSoFar"] = round(month_actual)
    out["remainingWorkingDays"] = remaining
    out["todayCommit"] = round((cfg["revenueTarget"] - month_actual) / remaining) if remaining else cfg["revenueTarget"]
    out["preMonth"] = False
    return out


# ── assembly ─────────────────────────────────────────────────────────────────
def tech_rows_for_window(w, fc_agg, sales_bucket, roster):
    entity_sales = w in ENTITY_SALES_WINDOWS
    rows = []
    for name, info in roster.items():
        fc = fc_agg.get(name)
        sb = sales_bucket.get(name, {"sales": 0.0, "n": 0}) if entity_sales else None
        opp = fc["opp"] if fc else 0
        converted = fc["converted"] if fc else 0
        jobs = fc["jobs"] if fc else 0
        rev = fc["rev"] if fc else 0.0
        leads = fc["leads"] if fc else 0
        membSold = fc["membSold"] if fc else 0
        membOpp = fc["membOpp"] if fc else 0
        sales_val = sb["sales"] if entity_sales else rev   # mtd/ytd fallback: FC report revenue
        if not (opp or jobs or sales_val or leads):
            continue
        membOppF = fc["membOpp"] if fc else 0
        oppW = fc["oppW"] if fc else 0.0
        row = {
            "name": name, "team": info["team"],
            "opps": int(opp), "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
            "avgSale": round(sales_val / converted) if converted else 0,
            "sales": round(sales_val), "salesIsApprox": not entity_sales,
            "membershipsSold": int(membSold), "membershipOpps": int(membOpp),
            "membershipConv": round(membSold / membOppF * 1000) / 10 if membOppF else 0,
            "leadsSet": leads,
            "revenue": round(rev), "jobs": int(jobs),
            "avgTicket": round(rev / jobs) if jobs else 0,
            "optionsPerOpp": round(oppW / opp, 2) if opp else 0,
        }
        rows.append(row)
    rows.sort(key=lambda r: -r["revenue"])
    return rows


def dept_summary(w, fc_agg, sales_dept, rev_bucket):
    entity_sales = w in ENTITY_SALES_WINDOWS
    opp = sum(a["opp"] for a in fc_agg.values())
    converted = sum(a["converted"] for a in fc_agg.values())
    jobs = sum(a["jobs"] for a in fc_agg.values())
    membOpp = sum(a["membOpp"] for a in fc_agg.values())
    membSold = sum(a["membSold"] for a in fc_agg.values())
    leads = sum(a["leads"] for a in fc_agg.values())
    rev = rev_bucket["total"]
    sales_val = sales_dept if entity_sales else sum(a["rev"] for a in fc_agg.values())
    return {
        "opps": int(opp), "converted": int(converted),
        "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
        "sales": round(sales_val), "salesIsApprox": not entity_sales,
        "avgSale": round(sales_val / converted) if converted else 0,
        "revenue": round(rev), "revenueBU": {BU_NAME[k]: round(v) for k, v in rev_bucket["bu"].items()},
        "jobs": int(jobs), "avgTicket": round(rev / jobs) if jobs else 0,
        "membershipsSold": int(membSold), "membershipOpps": int(membOpp),
        "membershipConv": round(membSold / membOpp * 1000) / 10 if membOpp else 0,
        "leadsSet": leads,
    }


# ── 10. week review (Sunday-evening prelim + Monday-morning final re-audit) ─
# Mon-Sun week containing YESTERDAY: a Sunday-evening run (cron in LA tz)
# still sees TODAY==that week's Sunday -> prelim; a Monday-or-later run sees
# TODAY > that week's Sunday -> final. --week YYYY-MM-DD overrides to review
# an arbitrary (already-Monday) week, e.g. for backfill/testing.
def bucket_sales_weeks(ests, job_tech, windows):
    """windows: {name: (frm, to)} -> ({name: {tech: {"sales":,"n":}}}, {name: dept_total})."""
    sales_by_tech = {w: defaultdict(lambda: {"sales": 0.0, "n": 0}) for w in windows}
    sales_dept = {w: 0.0 for w in windows}
    for e in ests:
        d = parse_ts(e.get("soldOn"))
        if not d:
            continue
        d = d.date()
        tech = job_tech.get(e.get("jobId"))
        if not tech:
            continue
        sub = num(e.get("subtotal"))
        for w, (frm, to) in windows.items():
            if frm <= d <= to:
                sales_dept[w] += sub
                sales_by_tech[w][tech]["sales"] += sub
                sales_by_tech[w][tech]["n"] += 1
    return sales_by_tech, sales_dept


def week_dept_summary(fc_agg, sales_dept_val, rev_total):
    opp = sum(a["opp"] for a in fc_agg.values())
    converted = sum(a["converted"] for a in fc_agg.values())
    jobs = sum(a["jobs"] for a in fc_agg.values())
    membOpp = sum(a["membOpp"] for a in fc_agg.values())
    membSold = sum(a["membSold"] for a in fc_agg.values())
    leads = sum(a["leads"] for a in fc_agg.values())
    oppW = sum(a["oppW"] for a in fc_agg.values())
    sales_val = sales_dept_val
    return {
        "opps": int(opp), "converted": int(converted),
        "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
        "sales": round(sales_val),
        "avgSale": round(sales_val / converted) if converted else 0,
        "revenue": round(rev_total),
        "jobs": int(jobs), "avgTicket": round(rev_total / jobs) if jobs else 0,
        "membershipsSold": int(membSold), "membershipOpps": int(membOpp),
        "membershipConv": round(membSold / membOpp * 1000) / 10 if membOpp else 0,
        "leadsSet": leads,
        "optionsPerOpp": round(oppW / opp, 2) if opp else 0,
    }


def week_tech_rows(fc_agg, sales_bucket, roster):
    rows = []
    for name, info in roster.items():
        fc = fc_agg.get(name)
        if not fc:
            continue
        sb = sales_bucket.get(name, {"sales": 0.0, "n": 0})
        opp, converted, jobs, rev = fc["opp"], fc["converted"], fc["jobs"], fc["rev"]
        leads, membSold, membOpp, oppW = fc["leads"], fc["membSold"], fc["membOpp"], fc["oppW"]
        sales_val = sb["sales"]
        if not (opp or jobs or sales_val or leads):
            continue
        rows.append({
            "name": name, "team": info["team"],
            "opps": int(opp), "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
            "avgSale": round(sales_val / converted) if converted else 0,
            "sales": round(sales_val),
            "membershipsSold": int(membSold), "membershipOpps": int(membOpp),
            "membershipConv": round(membSold / membOpp * 1000) / 10 if membOpp else 0,
            "leadsSet": leads, "revenue": round(rev), "jobs": int(jobs),
            "avgTicket": round(rev / jobs) if jobs else 0,
            "optionsPerOpp": round(oppW / opp, 2) if opp else 0,
        })
    rows.sort(key=lambda r: -r["revenue"])
    return rows


_WEEK_DELTA_KEYS = ["sales", "revenue", "opps", "converted", "closeRate", "avgSale",
                     "jobs", "avgTicket", "membershipsSold", "membershipConv",
                     "leadsSet", "optionsPerOpp"]


def week_deltas(cur, prior):
    return {k: round(cur.get(k, 0) - prior.get(k, 0), 2) for k in _WEEK_DELTA_KEYS}


def build_audit_note(prelim_dept, final_dept):
    """Human sentence quantifying what changed vs. Sunday's prelim pass."""
    parts = []
    sales_delta = round(final_dept.get("sales", 0) - prelim_dept.get("sales", 0))
    if sales_delta:
        parts.append("%s$%s sales" % ("+" if sales_delta >= 0 else "-", format(abs(sales_delta), ",")))
    mem_delta = final_dept.get("membershipsSold", 0) - prelim_dept.get("membershipsSold", 0)
    if mem_delta:
        parts.append("%s%d membership%s" % ("+" if mem_delta >= 0 else "-", abs(mem_delta),
                                              "" if abs(mem_delta) == 1 else "s"))
    if not parts:
        rev_delta = round(final_dept.get("revenue", 0) - prelim_dept.get("revenue", 0))
        if rev_delta:
            parts.append("%s$%s revenue" % ("+" if rev_delta >= 0 else "-", format(abs(rev_delta), ",")))
    if not parts:
        return "no material change from Sunday's preliminary pull"
    return " / ".join(parts) + " posted after Sunday review"


def week_review_main():
    week_arg = None
    if "--week" in sys.argv:
        week_arg = dt.date.fromisoformat(sys.argv[sys.argv.index("--week") + 1])
    log("start week-review" + (" [CLOUD]" if CLOUD else ""))

    wr_path = os.path.join(HERE, "weekreview.json")
    if CLOUD:
        cloud_bootstrap_creds()
        cloud_seed_files([(wr_path, "weekreview.json")])
        log("cloud: seeded weekreview.json from dashboard repo")

    try:
        wr = json.load(open(wr_path))
    except Exception:
        wr = {"weeks": {}, "latest": None}
    wr.setdefault("weeks", {})

    global _WR_AUDIT
    _WR_AUDIT = {k: v.get("audit") for k, v in wr["weeks"].items()}

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

    roster = fetch_roster()
    log("roster: %d techs" % len(roster))

    log("FC v1 report x2 (current + prior week)")
    cur_fc = fc_week_agg(monday, sunday, roster)
    prior_fc = fc_week_agg(prior_monday, prior_sunday, roster)

    log("sold estimates (sales$, entity-precise, both weeks)")
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": utc_iso(prior_monday)})
    ests = [e for e in ests if e.get("soldOn") and e.get("businessUnitId") in DEPT_BUS]
    job_ids = {e["jobId"] for e in ests if e.get("jobId")}
    job_tech = fetch_job_tech_map(job_ids, roster)
    week_windows = {"cur": (monday, sunday), "prior": (prior_monday, prior_sunday)}
    sales_by_tech, sales_dept = bucket_sales_weeks(ests, job_tech, week_windows)

    log("invoices (revenue$, both weeks)")
    invs = []
    for bu in DEPT_BUS:
        invs += paged("/accounting/v2/tenant/{tenant}/invoices",
                       {"businessUnitId": bu, "invoicedOnOrAfter": utc_iso(prior_monday)})
        time.sleep(0.3)
    cur_rev = revenue_for_range(invs, monday, sunday)
    prior_rev = revenue_for_range(invs, prior_monday, prior_sunday)

    cur_dept = week_dept_summary(cur_fc, sales_dept["cur"], cur_rev)
    prior_dept = week_dept_summary(prior_fc, sales_dept["prior"], prior_rev)
    cur_dept["deltas"] = week_deltas(cur_dept, prior_dept)
    cur_techs = week_tech_rows(cur_fc, sales_by_tech["cur"], roster)

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
        cloud_publish_files([(wr_path, "weekreview.json")])
        log("cloud: published weekreview.json to dashboard repo")

    log("DONE in %.1fs" % (time.time() - _T0))
    print("\nWeek %s (%s) audit=%s — %s" % (key, label, audit, auditNote))
    print("dept: sales $%s | revenue $%s | opps %d | close %.1f%% | membSold %d" % (
        format(cur_dept["sales"], ","), format(cur_dept["revenue"], ","),
        cur_dept["opps"], cur_dept["closeRate"], cur_dept["membershipsSold"]))
    print("deltas vs prior week:", cur_dept["deltas"])
    print("techs: %d rows; top 3:" % len(cur_techs),
          [(t["name"], t["revenue"], t["closeRate"]) for t in cur_techs[:3]])


# ── self-healing guards (2026-08-02 incident: GitHub native cron silently
# skipped the 5:35 AM full rebuild, Aug 2 06:46->14:09 UTC gap, so 3-min
# light runs kept republishing July's MTD block into August) ───────────────
def _light_escalation_reason(old):
    """Return a reason string if a --light run should be promoted to a full
    run, else None. Cheap: three dict lookups + one timestamp parse."""
    cur_month = MONTHS[TODAY.month - 1]
    if old.get("mtdMonth") != cur_month:
        return "mtd month stale (seeded=%r, current=%r)" % (old.get("mtdMonth"), cur_month)
    monthly = old.get("monthly") or []
    if not any(m.get("month") == cur_month for m in monthly):
        return "monthly[] missing current month %r" % cur_month
    lfr = old.get("lastFullRun")
    if not lfr:
        return "no lastFullRun stamp found"
    try:
        last = dt.datetime.fromisoformat(lfr)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        age_h = (NOW - last).total_seconds() / 3600
        if age_h > 26:
            return "lastFullRun stale (%.1fh old)" % age_h
    except Exception:
        return "lastFullRun stamp unparsable (%r)" % lfr
    return None


def _maybe_week_review_catchup():
    """Self-heal weekreview.yml's native crons from inside the 3-min light
    run: if it's past Sun 8:30pm PT and this week's prelim hasn't been
    written yet, run it inline; if it's past Mon 6:00am PT and this week is
    still audit=='prelim', finalize it inline. No-op the rest of the time
    (one time check); idempotent once weekreview.json reflects the right
    state (next light run's check naturally falls through)."""
    weekday = TODAY.weekday()   # Mon=0 .. Sun=6
    is_sun_eve = weekday == 6 and NOW.time() >= dt.time(20, 30)
    is_mon_am = weekday == 0 and NOW.time() >= dt.time(6, 0)
    if not (is_sun_eve or is_mon_am):
        return
    wr_path = os.path.join(HERE, "weekreview.json")
    if CLOUD:
        cloud_seed_files([(wr_path, "weekreview.json")])
    try:
        wr = json.load(open(wr_path))
    except Exception:
        wr = {"weeks": {}}
    ref = TODAY if is_sun_eve else (TODAY - dt.timedelta(days=1))
    monday = ref - dt.timedelta(days=ref.weekday())
    key = monday.isoformat()
    entry = (wr.get("weeks") or {}).get(key)
    if is_sun_eve and not entry:
        log("light run: week-review catch-up — %s prelim missing, running inline" % key)
        week_review_main()
    elif is_mon_am and entry and entry.get("audit") == "prelim":
        log("light run: week-review catch-up — %s still prelim past Mon 6am, finalizing inline" % key)
        week_review_main()
    else:
        log("light run: week-review catch-up — %s already %s, nothing to do" %
            (key, entry.get("audit") if entry else "missing (not yet due)"))


def main():
    light = "--light" in sys.argv
    log("start — roster" + (" [LIGHT]" if light else "") + (" [CLOUD]" if CLOUD else ""))

    path = os.path.join(HERE, "servicedata.json")
    hist_path_early = os.path.join(HERE, "servicedata_history.json")
    if CLOUD:
        cloud_bootstrap_creds()
        cloud_seed(path, hist_path_early)
        log("cloud: seeded servicedata.json / servicedata_history.json from dashboard repo")

    roster = fetch_roster()
    log("roster: %d techs (%s)" % (len(roster), ", ".join(sorted(set(v["team"] for v in roster.values())))))

    old = None
    if light:
        try:
            old = json.load(open(path))
        except Exception:
            log("WARNING: --light requested but no existing servicedata.json found — falling back to full run")
            light = False
    if light:
        reason = _light_escalation_reason(old)
        if reason:
            log("light run escalated to full: %s" % reason)
            light = False

    live_windows = {"today": WINDOWS["today"], "wtd": WINDOWS["wtd"]} if light else WINDOWS

    log("FC v1 report x%d window(s)" % len(live_windows))
    fc_by_window = {}
    for w, (frm, to) in live_windows.items():
        c, rows = fc_window(frm, to)
        fc_by_window[w] = fc_tech_rows(c, rows, roster)
        log("  %s: %d roster techs w/ rows" % (w, len(fc_by_window[w])))
        time.sleep(0.5)

    log("sold estimates (sales$, today/wtd entity-precise)")
    ests = fetch_sold_estimates_recent()
    job_ids = {e["jobId"] for e in ests if e.get("jobId")}
    log("  unique jobs to resolve tech for: %d" % len(job_ids))
    job_tech = fetch_job_tech_map(job_ids, roster)
    log("  resolved tech for %d jobs" % len(job_tech))
    sales_by_window, sales_dept = bucket_sales(ests, job_tech)

    if light:
        # small invoice pull: covers WTD (for revenue bucket) and, if today
        # falls inside the budget month, the MTD-so-far span for todayCommit.
        import calendar as _cal
        try:
            bcfg = json.load(open(os.path.join(HERE, "service_budget.json")))
            by, bm = (int(x) for x in bcfg["month"].split("-"))
            bstart = dt.date(by, bm, 1)
        except Exception:
            bstart = WTD_START
        fetch_from = min(WTD_START, bstart) if TODAY >= bstart else WTD_START
        log("invoices (light: revenue$ from %s)" % fetch_from)
        invs = []
        for bu in DEPT_BUS:
            invs += paged("/accounting/v2/tenant/{tenant}/invoices",
                           {"businessUnitId": bu, "invoicedOnOrAfter": utc_iso(fetch_from)})
            time.sleep(0.3)
        rev_by_window = bucket_revenue(invs, live_windows)
    else:
        log("invoices (revenue$, BU-filtered server-side)")
        invs = fetch_invoices()
        rev_by_window = bucket_revenue(invs)

    if light:
        monthly = old.get("monthly", [])
    else:
        log("monthly series")
        monthly = monthly_series(invs, roster)

    log("board counts (today)")
    board = board_counts()

    log("calls board (forward 5 non-Sunday days)")
    calls_path = os.path.join(HERE, "servicecalls.json")
    calls_hist_path = os.path.join(HERE, "servicecalls_history.json")
    if CLOUD:
        cloud_seed_files([(calls_path, "servicecalls.json"),
                           (calls_hist_path, "servicecalls_history.json")])
    calls_board = build_calls_board(roster)
    with open(calls_path + ".tmp", "w") as f:
        json.dump(calls_board, f, separators=(",", ":"))
    os.replace(calls_path + ".tmp", calls_path)
    calls_hist = update_calls_history(calls_board["days"], calls_hist_path)
    if CLOUD:
        cloud_publish_files([(calls_path, "servicecalls.json"),
                              (calls_hist_path, "servicecalls_history.json")])
    log("wrote %s (%d days, %d history snapshots)" % (calls_path, len(calls_board["days"]), len(calls_hist)))

    log("techDaily (current week day-by-day)")
    old_tech_daily = old.get("techDaily") if light else None
    tech_daily = build_tech_daily(roster, ests, job_tech, old_tech_daily=old_tech_daily, light=light)

    log("weekly series (9 weeks)")
    old_weekly = old.get("weekly") if light else None
    weekly = weekly_series(roster, invs, fc_by_window["wtd"], sales_dept.get("wtd", 0.0),
                            old_weekly=old_weekly, light=light)

    log("budget block")
    budget = budget_block(invs)

    out = {"updated": NOW.isoformat(), "techs": {}, "monthly": monthly, "board": board,
           "techDaily": tech_daily, "weekly": weekly, "budget": budget}
    # self-healing stamps (see _light_escalation_reason): mtdMonth/lastFullRun
    # only advance on a genuine full run; light runs carry the prior values
    # forward so a stale seed is detectable on the NEXT light run.
    if light:
        out["mtdMonth"] = (old or {}).get("mtdMonth")
        out["lastFullRun"] = (old or {}).get("lastFullRun")
    else:
        out["mtdMonth"] = MONTHS[TODAY.month - 1]
        out["lastFullRun"] = NOW.isoformat()
    for w in WINDOWS:
        if light and w in ("mtd", "ytd"):
            out[w] = old.get(w, {})
            out["techs"][w] = old.get("techs", {}).get(w, [])
            continue
        sd = sales_dept.get(w, 0.0)
        out[w] = dept_summary(w, fc_by_window[w], sd, rev_by_window[w])
        sb = sales_by_window.get(w, {})
        out["techs"][w] = tech_rows_for_window(w, fc_by_window[w], sb, roster)

    with open(path + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    log("wrote %s" % path)

    # per-day history snapshot, deduped on date — includes compact per-tech
    # {sales, opps, close, mem} for today (from techDaily) so day-by-day
    # survives past the current calendar week.
    hist_path = os.path.join(HERE, "servicedata_history.json")
    try:
        hist = json.load(open(hist_path))
    except Exception:
        hist = {}
    today_iso = TODAY.isoformat()
    techs_today = {}
    for name, rec in tech_daily.items():
        if name == "_days":
            continue
        cell = rec.get(today_iso)
        if cell:
            techs_today[name] = {"sales": cell["sales"], "opps": cell["opps"],
                                  "close": cell["closeRate"], "mem": cell["membershipsSold"]}
    hist[today_iso] = {"today": out["today"], "wtd": out["wtd"], "mtd": out["mtd"],
                        "ytd": out["ytd"], "board": board, "updated": NOW.isoformat(),
                        "techs": techs_today}
    with open(hist_path + ".tmp", "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    os.replace(hist_path + ".tmp", hist_path)
    log("wrote %s (%d days of history)" % (hist_path, len(hist)))

    if CLOUD:
        cloud_publish(path, hist_path)
        log("cloud: published servicedata.json / servicedata_history.json to dashboard repo")

    if light:
        try:
            _maybe_week_review_catchup()
        except Exception as ex:
            log("week-review catch-up skipped (error): %s" % ex)

    log("DONE in %.1fs" % (time.time() - _T0))
    print("\nToday: sales $%s | revenue $%s | opps %d | close %.1f%% | jobs %d" % (
        format(out["today"]["sales"], ","), format(out["today"]["revenue"], ","),
        out["today"]["opps"], out["today"]["closeRate"], out["today"]["jobs"]))
    print("MTD:   sales $%s | revenue $%s | opps %d | close %.1f%% | jobs %d | membSold %d" % (
        format(out["mtd"]["sales"], ","), format(out["mtd"]["revenue"], ","),
        out["mtd"]["opps"], out["mtd"]["closeRate"], out["mtd"]["jobs"], out["mtd"]["membershipsSold"]))
    print("board:", board)
    print("top 3 MTD techs:", [(t["name"], t["revenue"], t["closeRate"]) for t in out["techs"]["mtd"][:3]])


if __name__ == "__main__":
    if "--week-review" in sys.argv:
        week_review_main()
    else:
        main()
