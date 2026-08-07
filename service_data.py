#!/usr/bin/env python3
"""HVAC-Service department data engine for the new Service dashboard.

TWO POPULATIONS, deliberately (reworked 2026-08-06):
  DEPARTMENT-WIDE — every technician who ran work on business units HVAC -
    Service (333) or HVAC - Maintenance (342817560), roster or not. This is
    what the Overview / Department Pulse tiles, the 9-week weekly[] series and
    the monthly[] chart describe. Before this change they were roster-scoped
    and silently dropped the dispatch-team "1Silo" techs, under-reporting
    2026-08-05 by 41 of 126 jobs, $1,869 of revenue, and reading avg ticket
    $691 against the true $481.
  ROSTER (25-ish techs on dispatch teams "2a Service NO Sam Maintenance" +
    "2b Service with SAM Maintenance", fetched dynamically from
    settings/v2/technicians — never hardcoded). This is the By Tech table,
    techDaily, alerts and coaching. Unchanged.

Data sources (see REPORT BACK for calibration notes):
  - opps / close rate / leads set / options-per-opp / completed jobs /
    completed revenue <- Reporting API, Field Conversion Report v1
    (technician/328361546), the same report id ropp/service_live.py already
    calibrates against. Fetched once per window (today/wtd/mtd/ytd) and once
    per elapsed month, then split into a roster agg (fc_tech_rows) and a
    department agg (fc_dept_rows) from the SAME rows.
  - dept revenue + avg ticket <- that report's CompletedRevenueWithAdjustments
    over CompletedJobs: one population, completion basis, reproduces the
    Command Center deck exactly ($60,629 / 126 / $481 for 2026-08-05).
  - sales (sold-estimate $) <- sales/v2/estimates. The department total needs
    no tech attribution so it is pulled for the whole YTD span (28.7s); only
    the WTD slice gets the job -> firstAppointmentId ->
    dispatch/v2/appointment-assignments -> roster-tech join, which is the part
    that does not scale.
  - memberships sold / conversion / offer rate <- real counts:
    memberships/v2/memberships created that day joined to that day's completed
    dept jobs, over a denominator of completed dept jobs whose CUSTOMER did not
    already hold a membership covering that day. today/wtd/mtd only; YTD keeps
    the report's rounded proxy (flagged membershipsSoldIsApprox). The By Tech
    offer columns (today/wtd) are that SAME job set PARTITIONED by technician,
    so the table and the Overview tile answer one question — the roster/dept
    gap shows up as recon["unattributedJobs"], logged every run.
  - invoiced revenue (BU split + budget/pace card) <- accounting/v2/invoices,
    filtered to the two dept BUs, subTotal. Published as revenueBU /
    revenueInvoiced; this is a cash-posted basis and intentionally differs
    from the completion-basis REVENUE tile.
  - board (today only) <- jpm/v2/appointments for "on board" + jpm/v2/jobs
    completedOn for "ran".

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
import base64, json, os, re, sys, time, datetime as dt
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
            except Exception as ex:
                # non-fatal (callers fall back to a full run), but name it —
                # a silent seed failure looks exactly like "first run ever"
                log("WARNING: could not write seeded %s to %s: %r — this run will "
                    "behave as if the file were absent" % (repo_name, local_path, ex))


def cloud_publish_files(pairs):
    """Push each (local_path, repo_name) pair back to the dashboard repo,
    skip-if-unchanged vs. what cloud_seed_files() pulled at session start.

    An unreadable artifact is now a HARD failure. Every caller writes the file
    immediately before publishing it, so a read error here means the artifact
    this run was supposed to produce doesn't exist — and silently `continue`ing
    published nothing while the run still exited 0, leaving the dashboard on
    yesterday's data behind a green workflow. Go red instead."""
    for local_path, repo_name in pairs:
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                new_txt = f.read()
        except Exception as ex:
            log("PUBLISH FAILED: cannot read %s (target %s): %r" % (local_path, repo_name, ex))
            raise RuntimeError("refusing to exit 0 without publishing %s — %s unreadable: %r"
                               % (repo_name, local_path, ex))
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


# membership / SAM item pattern — verbatim port of servicefeed_sync.py's
# _is_membership_item() (sku.name starts with SAM/PLSAM, or displayName
# mentions Membership / Maintenance Agreement / Service Agreement) so both
# engines agree on what counts as a membership offer.
def _is_membership_item(sku):
    nm = (sku.get("name") or "").upper()
    disp = (sku.get("displayName") or "").lower()
    if nm.startswith("SAM") or nm.startswith("PLSAM"):
        return True
    return any(x in disp for x in ("membership", "maintenance agreement", "service agreement"))


# Sierra's own membership SKU codes are SAM01..SAM12 exactly. The department-
# wide membership OFFER detector uses this strict pattern rather than
# _is_membership_item()'s loose prefix/display-name test: at dept scope the
# loose test also catches Plumbing's PLSAM* and any "service agreement"-worded
# line item, which inflates the offer numerator against a denominator that is
# strictly HVAC-Service dept jobs. (Per-tech offer detection keeps the loose
# test so the By Tech column is unchanged — see bucket_membership_offers.)
SAM_SKU_RE = re.compile(r"^sam(0[1-9]|1[0-2])$", re.I)


def _is_sierra_sam_sku(sku):
    return bool(SAM_SKU_RE.match(((sku or {}).get("name") or "").strip()))


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ── person-name normalization ────────────────────────────────────────────────
# BUG FIX (2026-08-06): ServiceTitan stores some technician names with a
# TRAILING SPACE (10 of 142 active techs, e.g. "Tyler Battershell " — he is the
# only one on the 2A/2B roster today). The pad comes through on EVERY source
# that names a tech: settings/v2/technicians, dispatch/v2/appointment-
# assignments (technicianName) and the FC v1 report's Name column. fetch_roster
# and the FC aggregators happened to .strip(), but fetch_job_tech_map and
# build_calls_board compared the raw dispatch string against the stripped roster
# keys, so every one of Battershell's jobs failed the `name in roster` test and
# fell into the unattributed bucket: his By Tech row read $0 sales and 0
# membership-offer jobs while his FC-derived columns (jobs/revenue/opps) were
# fine — a half-populated row that looked like a real performance problem.
#
# Every roster comparison and every roster-keyed lookup now goes through
# norm_name() on BOTH sides. Whitespace only — collapse internal runs, strip the
# ends. DELIBERATELY NOT case-folded: verified 2026-08-06 that there are zero
# case-only twins among the 26 roster techs, the 142 active techs, or the FC
# Name column, so case-folding would buy nothing today while creating a real
# future hazard (two genuinely different people whose names differ only by case
# would silently merge into one row). Whitespace collapse was checked the same
# way — it collides no two distinct names in any of the three sources.
def norm_name(v):
    """Canonical form of a person name for roster matching (whitespace only)."""
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def utc_iso(d, end_of_day=False):
    """Local (Vegas) midnight of date d -> UTC 'Z' timestamp string.

    Correct for fields that carry a REAL instant (job.completedOn,
    estimate.soldOn/createdOn, appointment.start). WRONG as a lower bound for
    pure calendar-date fields — see date_lower_bound()."""
    t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
    local = dt.datetime.combine(d, t, tzinfo=TZ)
    return local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_lower_bound(d):
    """Server-side lower bound for filters on pure CALENDAR-DATE fields —
    accounting/v2/invoices' `invoicedOnOrAfter` in particular.

    BUG (found 2026-08-06): invoiceDate is a calendar date stamped at UTC
    midnight ('2026-08-01T00:00:00Z' means the day Aug 1 — see
    parse_date_only), but utc_iso(2026-08-01) emits '2026-08-01T07:00:00Z'
    (local midnight in UTC). 00:00Z < 07:00Z, so the server dropped EVERY
    invoice on the first day of the requested range: for Aug 1 that silently
    lost 56 invoices / $9,048 from the month.

    Fix: bound on the UTC-midnight stamp of the day BEFORE d. The extra day
    can never leak into a window total because every consumer re-buckets with
    the local-date comparison `frm <= parse_date_only(invoiceDate) <= to`,
    which stays authoritative."""
    return (d - dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


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
    # The Field Conversion report is the source of EVERY close-rate / opps /
    # membership figure on the Service dashboard. A valid response always
    # carries a field list even when the window has zero rows; an empty
    # `fields` means a malformed/empty 200 body, which fc_tech_rows() would
    # quietly turn into agg={} and publish as a confident all-zero day
    # (close 0%, opps 0, sales $0). Zero ROWS is still legal — a "today"
    # window at 5:35 AM genuinely has none.
    if not fields:
        raise RuntimeError(
            "report %s/%s returned no field list for %r — malformed/empty API "
            "response; refusing to publish it as a zero day" % (cat, rid, parameters))
    return fields, out


# ── 1. roster ────────────────────────────────────────────────────────────────
def fetch_roster():
    techs = paged("/settings/v2/tenant/{tenant}/technicians", {"active": "true"}, page_size=200)
    roster = {}
    for t in techs:
        team = t.get("team")
        if team not in TEAMS:
            continue
        key = norm_name(t.get("name"))
        if not key:
            log("WARNING: active technician id %s on team %r has a blank name — skipped"
                % (t.get("id"), team))
            continue
        if key in roster and roster[key]["id"] != t["id"]:
            # Two roster techs whose names differ only by whitespace would
            # silently merge into one By Tech row — name it rather than lose it.
            log("WARNING: roster name collision after whitespace normalization: %r "
                "(tech ids %s and %s) — keeping the first, the second's work will "
                "land in the first's row" % (key, roster[key]["id"], t["id"]))
            continue
        roster[key] = {"team": TEAMS[team], "id": t["id"]}
    return roster


# ── 1b. dept business-unit NAME resolution (FC report only exposes names) ──
# The Field Conversion report identifies the business unit by NAME
# (TechnicianBusinessUnit), never by id, so dept-scoping FC rows has to be
# name-based. Do NOT hardcode the strings: a rename in ServiceTitan would then
# match zero rows and silently publish an all-zero department. Resolve the
# names live from settings/v2/business-units keyed on the ID (333 /
# 342817560), which is the stable identifier, and keep the static BU_NAME map
# only as an offline fallback + as the (unchanged) revenueBU output labels.
#
# Note on "HVAC - Maintenance": BU 342817560 really IS named that, but
# TechnicianBusinessUnit is the TECH'S HOME business unit and no technician's
# home BU is Maintenance — so that half of the match has never fired and never
# will. It stays in the accepted set (harmless, and correct if staffing ever
# changes); the department's maintenance work is captured because the techs who
# run it are homed in HVAC - Service.
_BU_NAMES = None


def dept_bu_names():
    """Set of lowercased business-unit names that count as "the department"."""
    global _BU_NAMES
    if _BU_NAMES is not None:
        return _BU_NAMES
    live = {}
    try:
        for b in paged("/settings/v2/tenant/{tenant}/business-units", {}, page_size=200):
            if b.get("id") in DEPT_BUS and (b.get("name") or "").strip():
                live[b["id"]] = b["name"].strip()
    except Exception as ex:
        log("WARNING: business-unit name lookup failed (%r) — falling back to the "
            "static BU_NAME map; a BU rename would go undetected this run" % ex)
    for bu_id, static_name in BU_NAME.items():
        got = live.get(bu_id)
        if got is None:
            log("WARNING: business unit %s not returned by settings/v2/business-units — "
                "using static name %r" % (bu_id, static_name))
        elif got != static_name:
            log("NOTE: business unit %s renamed in ServiceTitan (%r -> %r) — matching on "
                "the live name; BU_NAME/revenueBU labels still read %r"
                % (bu_id, static_name, got, static_name))
    names = {v.strip().lower() for v in live.values()} | {v.strip().lower() for v in BU_NAME.values()}
    _BU_NAMES = names
    return _BU_NAMES


# ── 2. Field Conversion v1 report, per window + per month ──────────────────
def fc_window(frm, to):
    params = [{"name": "From", "value": frm.isoformat()}, {"name": "To", "value": to.isoformat()},
              {"name": "IncludeInactive", "value": False}]
    fields, rows = run_report_all(*FC_REPORT, params)
    c = {n: i for i, n in enumerate(fields)}
    return c, rows


def _fc_blank():
    return {"jobs": 0, "opp": 0.0, "converted": 0.0, "rev": 0.0,
            "leads": 0, "membSold": 0.0, "membOpp": 0.0, "oppW": 0.0}


def _fc_add(a, c, r):
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


def _fc_is_dept_row(c, r):
    return str(r[c["TechnicianBusinessUnit"]] or "").strip().lower() in dept_bu_names()


def fc_tech_rows(c, rows, roster):
    """dept-BU rows for ROSTER techs, summed per tech (a tech can have a
    Service row and a Maintenance row — both count toward the department).

    This is the By Tech / techDaily / coaching / alerts population and it is
    deliberately narrow: the 25-ish techs on dispatch teams 2A/2B. It is NOT
    the department — see fc_dept_rows()."""
    agg = {}
    for r in rows:
        name = norm_name(r[c["Name"]])
        if name not in roster or not _fc_is_dept_row(c, r):
            continue
        _fc_add(agg.setdefault(name, _fc_blank()), c, r)
    return agg


def fc_dept_rows(c, rows):
    """DEPARTMENT-WIDE: every FC row on a dept business unit, no roster filter.

    Added 2026-08-06. fc_tech_rows() drops every FC row whose technician isn't
    on the 2A/2B roster, and those dropped techs are real department labor
    (dispatch team "1Silo", BU 333 — apprentices, install-adjacent and helper
    techs). The Overview / Department Pulse tiles are supposed to describe the
    DEPARTMENT, so they read from here; roster-scoping them under-reported
    Aug 5 by 41 jobs ($1,869 revenue, avg ticket $691 vs the true $481) and
    made Leads Set read 1 for a week where the department set 82.

    Reproduces the authoritative Command Center deck exactly for 2026-08-05:
    jobs 126, CompletedRevenueWithAdjustments $60,629, avg ticket $481."""
    agg = {}
    for r in rows:
        if not _fc_is_dept_row(c, r):
            continue
        _fc_add(agg.setdefault(norm_name(r[c["Name"]]), _fc_blank()), c, r)
    return agg


def fc_totals(agg):
    """Sum a {name: fc_blank()} aggregate into one dict of department totals."""
    t = _fc_blank()
    for a in agg.values():
        for k in t:
            t[k] += a[k]
    return t


# ── 3. sold estimates (sales$) — entity precision.
# The raw pull is company-wide (every BU, every department) since the API has
# no server-side technician OR businessUnit filter on this endpoint, so the
# dept-BU filter is applied client-side.
#
# BUG FIX (2026-07-30): bucket_sales() used to sum every row's subtotal into
# the dept total regardless of businessUnitId or tech attribution — on the week
# this was caught, of 326 rows only 244 were even on a dept BU (333/342817560);
# the other 82 were BU 370/353/354/595105985/340802904 (Install and other
# non-Service-dept business units) worth ~$964k, e.g. a single $55,160 estimate
# (id 669275198, BU 370, soldOn 2026-07-28) that has nothing to do with the
# HVAC-Service department. That inflated wtd.sales to $1.07M against a
# techDaily/per-tech sum of ~$105k.
#
# COST NOTE (2026-08-06): the expensive part of a long span was never this
# pull (measured 28.7s for the whole YTD, 12,265 rows) — it was the
# job -> firstAppointmentId -> appointment-assignments -> tech join needed for
# PER-TECH attribution, which is unbounded. So: pull the long span once for
# the DEPARTMENT sales total (no join required), and hand only the WTD slice
# to fetch_job_tech_map() for per-tech attribution. Per-tech MTD/YTD sales
# still fall back to the FC report's CompletedRevenueWithAdjustments
# ("salesIsApprox") exactly as before — the By Tech table is unchanged.
def fetch_sold_estimates_dept(frm):
    """Dept-BU estimates sold on/after `frm`. No tech join — department total only."""
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": utc_iso(frm)})
    ests = [e for e in ests if e.get("soldOn") and e.get("businessUnitId") in DEPT_BUS]
    log("sold estimates since %s (dept BUs only): %d rows" % (frm, len(ests)))
    return ests


def recent_sold_estimates(dept_ests):
    """The WTD_START..TODAY slice of fetch_sold_estimates_dept() — the only
    part that gets the per-tech job->assignment join."""
    out = []
    for e in dept_ests:
        d = parse_ts(e.get("soldOn"))
        if d and d.date() >= WTD_START:
            out.append(e)
    log("  ...of which WTD-to-date (per-tech attribution set): %d rows" % len(out))
    return out


# ── 3b. membership OFFER detection — entity precision. An OFFER = a
# membership/SAM item present on ANY estimate for a job, sold or unsold — the
# FC report's MembershipConversionRate can't see unsold estimates, so this
# distinguishes "tech never offered" from "tech offered and lost the sale".
# Per-tech detection uses a verbatim port of servicefeed_sync.py's
# _is_membership_item() so both engines agree; the DEPARTMENT figure uses the
# stricter SAM01..SAM12 SKU test (see SAM_SKU_RE).
def fetch_membership_offer_estimates(frm):
    """All (sold or unsold) dept-BU estimates created on/after `frm`.
    createdOnOrAfter verified live against sales/v2/estimates (2026-08-04
    probe): returns creation-date-filtered rows including unsold ones, unlike
    soldAfter."""
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"createdOnOrAfter": utc_iso(frm)})
    ests = [e for e in ests if e.get("businessUnitId") in DEPT_BUS]
    log("membership-offer estimates since %s (dept BUs only): %d rows" % (frm, len(ests)))
    return ests


# ── 3c. tech CALL universe — SUPERSEDED 2026-08-06, NO LONGER CALLED BY main().
#
# fetch_tech_call_universe() / bucket_call_universe() / bucket_membership_offers()
# built the OLD per-tech membership-offer denominator: dept-BU appointments in
# the window, minus job types whose NAME looks SAM-covered or part-install. That
# was a job-type PROXY for "the customer isn't already a member". The department
# tile now uses the real thing — completed dept jobs whose CUSTOMER held no
# membership covering that day (build_membership_dept) — so the By Tech column
# was answering a different question than the tile directly above it. As of
# 2026-08-06 the per-tech offer numbers are partitioned out of that SAME
# non-member completed-job set (build_membership_dept(tech_windows=...)), and
# these three functions are retained only for their calibration history and in
# case the proxy is ever needed again. Dropping the call also removed a ~12s
# company-wide appointment pull from every run.
#
# Original notes follow.
#
# Entity precision, Today/WTD only. The
# membershipOfferRate denominator must come from the SAME population type as
# the numerator (entity jobs, not the FC report's "Opportunity" definition —
# those are two different job sets and mixing them produced >100% offer rates
# on 2026-08-04). Denominator = every dept-BU job with an appointment in the
# window, whether or not the tech ever built an estimate on it (a tech who
# built nothing offered nothing, and that must count against him). Same
# appointment -> job -> BU join board_counts()/build_calls_board() already use.
#
# EXCLUSIONS (2026-08-04, second pass): a raw "every dept appointment" count
# capped the dept rate at ~36% even for a perfectly-pitching team, because
# most maintenance visits are SAM membership-COVERED — the customer already
# has a membership, there's nothing to offer. Two carve-outs, narrower than
# build_calls_board()'s is_maint flag (which lumps all BU_MAINT + tune-ups
# together — too broad here, plain tune-ups ARE real membership opportunities
# and must stay in the denominator):
#   - SAM-covered visit: job type name contains "sam" (e.g. "SAM Cooling
#     Service (1 System) 4-7 Yrs") — no offer possible, member already covered.
#   - part-install: job type name contains both "part" and "install" — a
#     parts drop-off, not a sales call (same exemption dashboard alerts use).
def _is_sam_covered_jobtype(jt_name):
    return "sam" in (jt_name or "").lower()


def _is_part_install_jobtype(jt_name):
    nm = (jt_name or "").lower()
    return "part" in nm and "install" in nm


def fetch_tech_call_universe():
    """dept-BU appointments (not canceled) starting in WTD_START..TODAY, minus
    SAM-covered-visit and part-install job types (see module doc above).
    Returns (dept_appts, excl_counts) where excl_counts = {"sam": n, "partInstall": n}
    for the sanity-check report."""
    frm = dt.datetime.combine(WTD_START, dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    to = dt.datetime.combine(TODAY + dt.timedelta(days=1), dt.time.min, tzinfo=TZ).astimezone(dt.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    appts = paged("/jpm/v2/tenant/{tenant}/appointments", {"startsOnOrAfter": iso(frm), "startsBefore": iso(to)})
    appts = [a for a in appts if a.get("status") != "Canceled"]
    job_ids = sorted({a["jobId"] for a in appts if a.get("jobId")})
    jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", job_ids)}
    jts = fetch_job_types()
    dept_appts = [a for a in appts if jobs.get(a.get("jobId"), {}).get("businessUnitId") in DEPT_BUS]

    kept, excl_sam, excl_pi = [], 0, 0
    for a in dept_appts:
        j = jobs.get(a.get("jobId"), {})
        jt_name = jts.get(j.get("jobTypeId"), "") or ""
        if _is_sam_covered_jobtype(jt_name):
            excl_sam += 1
            continue
        if _is_part_install_jobtype(jt_name):
            excl_pi += 1
            continue
        kept.append(a)

    log("tech call universe: %d dept-BU appts kept (of %d total, %d jobs) — "
        "excluded %d SAM-covered, %d part-install" %
        (len(kept), len(appts), len(job_ids), excl_sam, excl_pi))
    return kept, {"sam": excl_sam, "partInstall": excl_pi}


def bucket_call_universe(dept_appts, job_tech):
    """{window: {tech: set(jobIds run)}}, {window: set(jobIds run)} — every
    dept-BU job a tech ran in the window (appointment-scoped), the
    membershipOfferRate DENOMINATOR. Bucketed on the appointment's start date."""
    out = {w: defaultdict(set) for w in ENTITY_SALES_WINDOWS}
    dept = {w: set() for w in ENTITY_SALES_WINDOWS}
    for a in dept_appts:
        jid = a.get("jobId")
        if not jid:
            continue
        d = parse_ts(a.get("start"))
        if not d:
            continue
        d = d.date()
        tech = job_tech.get(jid)
        for w in ENTITY_SALES_WINDOWS:
            frm, to = WINDOWS[w]
            if frm <= d <= to:
                dept[w].add(jid)
                if tech:
                    out[w][tech].add(jid)
    return out, dept


def fetch_job_tech_map(job_ids, roster, known_jobs=None, all_techs=None):
    """job id -> roster tech name, via job.firstAppointmentId -> appointment-assignments.

    known_jobs: {jobId: job} already in hand — those ids skip the /jpm/v2/jobs
      re-fetch. This is what lets the PER-TECH membership partition ride along
      on the department pass (fetch_completed_dept_jobs already returned the
      full job objects, firstAppointmentId included) instead of paying for a
      second jobs pull.
    all_techs: optional dict, filled with {jobId: [every active ROSTER tech on
      the first appointment]} so callers can report multi-tech jobs. The
      returned map still holds exactly ONE tech per job (the first active roster
      assignment), so a job can never be counted twice across techs."""
    job_ids = sorted(set(job_ids))
    known = known_jobs or {}
    jobs = [known[i] for i in job_ids if i in known]
    missing = [i for i in job_ids if i not in known]
    if missing:
        jobs += chunked_get("/jpm/v2/tenant/{tenant}/jobs", missing)
    appt_ids = [j["firstAppointmentId"] for j in jobs if j.get("firstAppointmentId")]
    appt_to_job = {j["firstAppointmentId"]: j["id"] for j in jobs if j.get("firstAppointmentId")}
    asg = chunked_get("/dispatch/v2/tenant/{tenant}/appointment-assignments", appt_ids, key="appointmentIds")
    job_tech = {}
    for a in asg:
        if not a.get("active"):
            continue
        name = norm_name(a.get("technicianName"))
        if name not in roster:
            continue
        jid = a.get("jobId") or appt_to_job.get(a.get("appointmentId"))
        if not jid:
            continue
        if all_techs is not None and name not in all_techs.setdefault(jid, []):
            all_techs[jid].append(name)
        if jid not in job_tech:
            job_tech[jid] = name
    return job_tech


ENTITY_SALES_WINDOWS = ("today", "wtd")   # per-TECH entity attribution windows


def bucket_sales(ests, job_tech):
    """{window: {tech: {"sales": $, "n": count}}} — today/wtd only (entity-precise).
    BUG FIX (2026-07-30): dept[w] now only accumulates rows that resolved to a
    roster tech (same condition as the per-tech buckets and as techDaily's
    bucket_sales_by_day) so weekly[current].sales reconciles EXACTLY with the
    sum of techs.wtd[].sales and techDaily weekTotals — previously it summed
    every dept-BU estimate even when job->tech attribution failed, silently
    double-counting against no tech.

    NOTE: the returned `dept` total is ROSTER-attributed, not department-wide.
    It feeds weekly[current].sales / the techDaily reconciliation only. The
    Overview SALES tile uses bucket_sales_dept()."""
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


def bucket_sales_dept(dept_ests, windows):
    """{window: sold-estimate subtotal $} DEPARTMENT-WIDE — every dept-BU
    estimate sold in the window, whether or not it attributes to a roster
    tech. This is what the Overview SALES tile reads.

    Verified against the Command Center deck for 2026-08-05: $35,615 exactly
    (78 dept sold estimates), vs $30,199 roster-attributed."""
    out = {w: 0.0 for w in windows}
    for e in dept_ests:
        d = parse_ts(e.get("soldOn"))
        if not d:
            continue
        d = d.date()
        sub = num(e.get("subtotal"))
        for w, (frm, to) in windows.items():
            if frm <= d <= to:
                out[w] += sub
    return out


def bucket_membership_offers(ests, job_tech, call_dept):
    """{window: {tech: set(jobIds offered)}}, {window: set(jobIds offered)},
    {window: {tech: set(jobIds sold-on-offer)}}, {window: set(jobIds sold-on-offer)}
    — today/wtd only (entity-precise, same budget as bucket_sales). A job counts
    as "offered" in a window if ANY of its estimates created in that window's
    date range carries a membership/SAM item, sold or not. Bucketed on the
    estimate's createdOn date (mirrors bucket_sales' use of soldOn).

    A job additionally counts as "sold-on-offer" if that same membership-
    carrying estimate is SOLD, using the identical test servicefeed_sync.py
    uses for membership_sold_job: soldOn present, or status name == "sold".
    This numerator (membershipSoldOnOffer) MUST be built from this same
    entity population as membershipOffered — it cannot come from membSold
    (the Field Conversion report figure used elsewhere for membershipsSold),
    because that report counts a different, broader population and is a
    rounded product (opportunities x report conversion rate). Pairing FC-
    report membSold with this entity-derived offered count produced
    close-on-offer rates over 100% (e.g. Chris Thomas 1 offered / 6 "sold" =
    600%) — found 2026-08-04. Only a same-population subset is a valid
    numerator for a close-ON-OFFER rate.

    call_dept = {window: set(jobIds)} from bucket_call_universe() — the SAME
    denominator population used for membershipOfferJobs. Offers are gated to
    jid in call_dept[w] so "offered" is a construction-time STRICT SUBSET of
    "ran" (offered <= offerJobs always holds; can't offer on a job the tech
    didn't run this window). See fetch_tech_call_universe() doc for why the
    numerator and denominator must share one job population."""
    out = {w: defaultdict(set) for w in ENTITY_SALES_WINDOWS}
    dept = {w: set() for w in ENTITY_SALES_WINDOWS}
    sold_out = {w: defaultdict(set) for w in ENTITY_SALES_WINDOWS}
    sold_dept = {w: set() for w in ENTITY_SALES_WINDOWS}
    for e in ests:
        items = e.get("items") or []
        skus = [(i.get("sku") or {}) for i in items]
        if not any(_is_membership_item(s) for s in skus):
            continue
        jid = e.get("jobId")
        if not jid:
            continue
        d = parse_ts(e.get("createdOn"))
        if not d:
            continue
        d = d.date()
        tech = job_tech.get(jid)
        st_name = ((e.get("status") or {}).get("name") or "").lower()
        sold = bool(e.get("soldOn")) or st_name == "sold"
        for w in ENTITY_SALES_WINDOWS:
            frm, to = WINDOWS[w]
            if frm <= d <= to and jid in call_dept.get(w, ()):
                dept[w].add(jid)
                if tech:
                    out[w][tech].add(jid)
                if sold:
                    sold_dept[w].add(jid)
                    if tech:
                        sold_out[w][tech].add(jid)
    return out, dept, sold_out, sold_dept


# ── 3e. DEPARTMENT-WIDE membership entity pass ─────────────────────────────
# Replaces the FC report's rounded `round(Opportunity x MembershipConversionRate)`
# proxy for the department tiles with real counts:
#
#   ran            = dept-BU jobs with jobStatus == "Completed" whose
#                    completedOn falls on the local day (the authoritative
#                    "Ran" definition; reproduces the deck's 126 for Aug 5,
#                    split 92 demand / 34 maintenance).
#   nonMemberJobs  = of those, the jobs whose CUSTOMER did not already hold a
#                    membership covering that day. This replaces the old
#                    job-type proxy (drop "SAM"-named job types + part
#                    installs), which only approximated "already covered".
#   sold           = memberships created that local day whose customer had one
#                    of that day's completed dept jobs.
#   offered        = nonMemberJobs that carry a SAM01..SAM12 SKU on ANY
#                    estimate created that day, plus any that converted (a
#                    sale is an offer).
#
# Windows: today/wtd always; mtd on FULL runs only. A YTD pass is NOT
# affordable — measured 2026-08-06: the YTD completed-jobs pull alone is 162s
# and the per-customer membership-coverage lookup would be 246 chunks on top
# of ~200s of created-estimate paging (~8 min). YTD therefore keeps the FC
# report approximation and is flagged membershipsSoldIsApprox: true.
def fetch_completed_dept_jobs(frm, to):
    """Dept-BU jobs completed (jobStatus == Completed) between local dates
    frm..to inclusive. completedOn is a real instant, so utc_iso() is the
    correct bound here (unlike invoiceDate — see date_lower_bound)."""
    a = utc_iso(frm)
    b = utc_iso(to + dt.timedelta(days=1))
    jobs = paged("/jpm/v2/tenant/{tenant}/jobs",
                 {"completedOnOrAfter": a, "completedBefore": b})
    out = [j for j in jobs
           if j.get("businessUnitId") in DEPT_BUS and j.get("jobStatus") == "Completed"]
    log("completed dept jobs %s..%s: %d (of %d company-wide)" % (frm, to, len(out), len(jobs)))
    return out


def fetch_memberships_created(frm, to):
    """Memberships CREATED between local dates frm..to inclusive (company-wide;
    dept attribution is done by joining to that day's completed dept jobs, not
    by membership.businessUnitId — a Service tech's sale can post against a
    different selling BU)."""
    ms = paged("/memberships/v2/tenant/{tenant}/memberships",
               {"createdOnOrAfter": utc_iso(frm),
                "createdBefore": utc_iso(to + dt.timedelta(days=1))})
    log("memberships created %s..%s: %d" % (frm, to, len(ms)))
    return ms


def fetch_membership_coverage(customer_ids):
    """{customerId: [(from_date, to_date_or_None), ...]} for every membership
    those customers have ever held. Uses the customerIds= filter (verified
    working 2026-08-06) — a full-tenant membership pull is 146,189 rows and is
    never an option."""
    ids = sorted({c for c in customer_ids if c})
    rows = chunked_get("/memberships/v2/tenant/{tenant}/memberships", ids,
                       key="customerIds", size=50)
    cov = defaultdict(list)
    for m in rows:
        cid = m.get("customerId")
        if cid:
            cov[cid].append((parse_date_only(m.get("from")), parse_date_only(m.get("to"))))
    log("membership coverage: %d rows for %d customers (%d chunks)"
        % (len(rows), len(ids), (len(ids) + 49) // 50))
    return cov


def _already_member(cov, cid, d):
    """True if the customer held a membership that covered the day BEFORE d —
    started strictly before d and had not ended by d.

    Point-in-time by DATE, deliberately not by the membership's current
    `status`: status is a live snapshot, so a membership that was active on
    a past day but has since been cancelled would wrongly re-open that day's
    denominator on every future rebuild."""
    for frm, to in cov.get(cid, ()):
        if frm and frm < d and (to is None or to >= d):
            return True
    return False


def build_membership_dept(windows, jobs, cov, memberships, offer_ests,
                          job_tech=None, tech_windows=()):
    """(dept, per_tech, recon)

    dept     {window: {"sold": n, "nonMemberJobs": n, "offered": n, "soldOnOffer": n}}
    per_tech {window: {tech: {"nonMemberJobs": n, "offered": n, "soldOnOffer": n}}}
             for the windows named in `tech_windows` (needs `job_tech`).
    recon    {window: {"deptNonMemberJobs", "techNonMemberJobs", "unattributedJobs"}}

    Everything is computed DAY BY DAY (membership status is evaluated as of the
    job's own completion day) and then unioned into each window, so a window
    total can never disagree with the days it contains.

    PER-TECH PARTITION (2026-08-06). The By Tech offer column used to run off a
    job-type proxy denominator (dept appointments minus SAM-named/part-install
    job types — see the SUPERSEDED block at 3c), so the tile and the table
    answered different questions. Now the per-tech numbers are literally a
    PARTITION of the department's own numbers: the same completed dept-BU jobs,
    the same point-in-time non-member test, the same strict SAM01..SAM12 offer
    test, the same "a sale is an offer" rule — just split by the job's
    technician (job.firstAppointmentId -> first active roster assignment, the
    attribution convention used everywhere else in this file).

    The split is not lossless and is not meant to be: a dept job whose first
    appointment carried no ACTIVE ROSTER technician (off-roster dept labour —
    the "1Silo" apprentice/helper techs — or an unassigned appointment) has no
    By Tech row to land in. Those jobs stay in the DEPARTMENT denominator and
    are counted in recon["unattributedJobs"] so main() can log the gap instead
    of losing it silently. Sum(per-tech) + unattributed == dept, exactly."""
    jobs_by_day = defaultdict(list)
    for j in jobs:
        ts = parse_ts(j.get("completedOn"))
        if ts:
            jobs_by_day[ts.date()].append(j)

    memb_by_day = defaultdict(list)
    for m in memberships:
        ts = parse_ts(m.get("createdOn"))
        if ts:
            memb_by_day[ts.date()].append(m)

    offer_jobs_by_day = defaultdict(set)
    for e in offer_ests:
        jid = e.get("jobId")
        ts = parse_ts(e.get("createdOn"))
        if not jid or not ts:
            continue
        if any(_is_sierra_sam_sku(i.get("sku")) for i in (e.get("items") or [])):
            offer_jobs_by_day[ts.date()].add(jid)

    acc = {w: {"sold": 0, "nonMemberJobs": set(), "offered": set(), "soldOnOffer": set()}
           for w in windows}
    tw = [w for w in tech_windows if w in windows]
    tacc = {w: defaultdict(lambda: {"nonMemberJobs": set(), "offered": set(),
                                    "soldOnOffer": set()}) for w in tw}
    unattr = {w: set() for w in tw}
    for d, day_jobs in jobs_by_day.items():
        wins = [w for w, (frm, to) in windows.items() if frm <= d <= to]
        if not wins:
            continue
        day_custs = {j.get("customerId") for j in day_jobs if j.get("customerId")}
        nonmem = [j for j in day_jobs if not _already_member(cov, j.get("customerId"), d)]
        nonmem_ids = {j["id"] for j in nonmem}
        nonmem_custs = {j.get("customerId") for j in nonmem if j.get("customerId")}
        day_ms = memb_by_day.get(d, [])
        sold_n = sum(1 for m in day_ms if m.get("customerId") in day_custs)
        sold_custs = {m.get("customerId") for m in day_ms if m.get("customerId") in nonmem_custs}
        converted_ids = {j["id"] for j in nonmem if j.get("customerId") in sold_custs}
        offered_ids = (offer_jobs_by_day.get(d, set()) & nonmem_ids) | converted_ids
        for w in wins:
            a = acc[w]
            a["sold"] += sold_n
            a["nonMemberJobs"] |= nonmem_ids
            a["offered"] |= offered_ids
            a["soldOnOffer"] |= converted_ids
            if w not in tacc:
                continue
            for jid in nonmem_ids:
                tech = (job_tech or {}).get(jid)
                if not tech:
                    unattr[w].add(jid)
                    continue
                t = tacc[w][tech]
                t["nonMemberJobs"].add(jid)
                if jid in offered_ids:
                    t["offered"].add(jid)
                if jid in converted_ids:
                    t["soldOnOffer"].add(jid)

    dept = {w: {"sold": a["sold"], "nonMemberJobs": len(a["nonMemberJobs"]),
                "offered": len(a["offered"]), "soldOnOffer": len(a["soldOnOffer"])}
            for w, a in acc.items()}
    per_tech = {w: {name: {k: len(v) for k, v in rec.items()}
                    for name, rec in techs.items()}
                for w, techs in tacc.items()}
    recon = {w: {"deptNonMemberJobs": dept[w]["nonMemberJobs"],
                 "techNonMemberJobs": sum(r["nonMemberJobs"] for r in per_tech[w].values()),
                 "deptOffered": dept[w]["offered"],
                 "techOffered": sum(r["offered"] for r in per_tech[w].values()),
                 "unattributedJobs": len(unattr[w])} for w in tw}
    return dept, per_tech, recon


def board_ran_by_bu(jobs, day):
    """{BU_SERVICE: n, BU_MAINT: n} of dept jobs COMPLETED on `day` — the
    authoritative "Ran" definition (job-level completedOn), not the old
    appointment-level `status == Done or job Completed` count which
    over-reported demandRan by 3 on 2026-08-05 (95 vs the deck's 92)."""
    out = {BU_SERVICE: 0, BU_MAINT: 0}
    for j in jobs:
        ts = parse_ts(j.get("completedOn"))
        if ts and ts.date() == day and j.get("businessUnitId") in out:
            out[j["businessUnitId"]] += 1
    return out


# ── 4. invoices (revenue$, BU split) — YTD span, bucketed ──────────────────
def fetch_invoices():
    """Server-side businessUnitId filter (confirmed to work) keeps this to just
    the two dept BUs instead of a company-wide invoice pull."""
    invs = []
    for bu in DEPT_BUS:
        invs += paged("/accounting/v2/tenant/{tenant}/invoices",
                       {"businessUnitId": bu, "invoicedOnOrAfter": date_lower_bound(YTD_START)})
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
# "sales" here is the FC v1 report's CompletedRevenueWithAdjustments sum since
# a per-month entity sold-estimate pull x7 months company-wide would blow the
# runtime budget. "revenue" (BU split) is entity-precise from the invoices
# pull, which already covers the full YTD span.
#
# 2026-08-06: switched from fc_tech_rows() (25-tech roster) to fc_dept_rows()
# (whole department). This chart sits directly under the Overview dept tiles;
# leaving it roster-scoped meant the August bar read 59.7% close / $58.8k while
# the MTD tile above it read 39.6% / $60.6k for the same days.
def monthly_series(invs):
    import calendar
    out = []
    for m in range(1, TODAY.month + 1):
        m_from = dt.date(YEAR, m, 1)
        last = calendar.monthrange(YEAR, m)[1]
        m_to = TODAY if m == TODAY.month else dt.date(YEAR, m, last)
        c, rows = fc_window(m_from, m_to)
        t = fc_totals(fc_dept_rows(c, rows))
        opp, conv = t["opp"], t["converted"]
        sales_approx = t["rev"]
        membOpp, membSold = t["membOpp"], t["membSold"]
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
def board_counts(completed_jobs_today=None):
    """ON BOARD = today's non-cancelled dept appointments (unchanged).

    RAN = dept jobs whose jobStatus is Completed and whose completedOn lands on
    TODAY, split by business unit. Changed 2026-08-06: the old rule counted an
    APPOINTMENT as ran when `status == "Done" or job.jobStatus == "Completed"`,
    which double-counts multi-appointment jobs and credits a job completed on
    a different day. For 2026-08-05 the old rule gave demandRan 95 against the
    authoritative deck's 92; the job-level rule gives 92 / 34 exactly and its
    total (126) now agrees with the dept jobs tile."""
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
        bu = jobs.get(a.get("jobId"), {}).get("businessUnitId")
        if bu == BU_SERVICE:
            board["demandOnBoard"] += 1
        elif bu == BU_MAINT:
            board["maintenanceOnBoard"] += 1
    if completed_jobs_today is None:
        completed_jobs_today = [j for j in jobs.values() if j.get("jobStatus") == "Completed"]
    ran = board_ran_by_bu(completed_jobs_today, TODAY)
    board["demandRan"] = ran[BU_SERVICE]
    board["maintenanceRan"] = ran[BU_MAINT]
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


def fc_week_aggs(monday, sunday, roster):
    """(roster_agg, dept_agg) for an arbitrary Mon-Sun week, cached to
    scratchpad forever UNLESS that week is currently tracked in weekreview.json
    with audit=='prelim' (see _WR_AUDIT note above).

    Cache payloads written before 2026-08-06 have only "_tech_agg" (roster) and
    are treated as a MISS so the dept-wide half gets populated — a stale
    roster-only cache would otherwise silently keep the 9-week Department Pulse
    chart on the old narrow population."""
    cache = _weekly_cache_path(monday)
    allow_cache = _week_cache_allowed(monday)
    if allow_cache and os.path.exists(cache):
        try:
            payload = json.load(open(cache))
            if isinstance(payload, dict) and "_tech_agg" in payload and "_dept_agg" in payload:
                return payload["_tech_agg"], payload["_dept_agg"]
        except Exception:
            pass
    c, rows = fc_window(monday, sunday)
    agg = fc_tech_rows(c, rows, roster)
    dept_agg = fc_dept_rows(c, rows)
    if allow_cache:
        try:
            with open(cache + ".tmp", "w") as f:
                json.dump({"_tech_agg": agg, "_dept_agg": dept_agg}, f)
            os.replace(cache + ".tmp", cache)
        except Exception:
            pass
    return agg, dept_agg


def fc_week_agg(monday, sunday, roster):
    """Roster-scoped week agg — week_review_main()'s per-tech population."""
    return fc_week_aggs(monday, sunday, roster)[0]


def fetch_fc_week_dept(monday, sunday, roster):
    """DEPARTMENT-WIDE week totals for the 9-week Department Pulse chart."""
    return fc_totals(fc_week_aggs(monday, sunday, roster)[1])


def weekly_series(roster, invs_for_revenue, current_wtd_dept_agg, current_wtd_sales_dept,
                   old_weekly=None, light=False):
    """9-week Department Pulse series. DEPARTMENT-WIDE as of 2026-08-06 (was
    roster-scoped) so it agrees with the Overview tiles directly above it;
    `sales` for the current week is the dept-wide entity sold-estimate total,
    past weeks keep the FC CompletedRevenueWithAdjustments approximation."""
    weeks = []
    for n_back in range(8, -1, -1):
        monday = WTD_START - dt.timedelta(weeks=n_back)
        sunday = monday + dt.timedelta(days=6)
        is_current = (n_back == 0)
        label = "%d/%d-%d/%d" % (monday.month, monday.day, sunday.month, sunday.day)
        if is_current:
            label += " (WTD)"
            t = fc_totals(current_wtd_dept_agg)
            opp, converted = t["opp"], t["converted"]
            membOpp, membSold = t["membOpp"], t["membSold"]
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
            nm = norm_name(a.get("technicianName"))
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
    except FileNotFoundError:
        log("budget block: no service_budget.json — publishing budget:null (UI hides the card)")
        return None
    except Exception as ex:
        # present but unreadable is NOT the same as "no budget configured"
        log("WARNING: service_budget.json exists but could not be parsed (%r) — "
            "publishing budget:null; the budget card will silently disappear" % ex)
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
def tech_rows_for_window(w, fc_agg, sales_bucket, roster, offer_bucket=None, call_bucket=None,
                          sold_bucket=None, memb_tech=None):
    """One By Tech table (ROSTER-scoped, deliberately — see fc_tech_rows).

    memb_tech: build_membership_dept()'s per-tech partition for this window,
      {tech: {"nonMemberJobs", "offered", "soldOnOffer"}}. When present it is
      the source of the membershipOffer* fields, so the By Tech offer column
      and the Overview offer tile are the same arithmetic on the same job set
      (2026-08-06). offer_bucket/call_bucket/sold_bucket are the SUPERSEDED
      job-type-proxy path, kept only as a fallback signature."""
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
        mrow = (memb_tech or {}).get(name)
        # `or mrow[...]`: a tech with non-member completed jobs but no FC row
        # must still get a row, otherwise his share of the department offer
        # denominator would vanish from the table without a trace.
        if not (opp or jobs or sales_val or leads or (mrow and mrow["nonMemberJobs"])):
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
        if memb_tech is not None or (offer_bucket is not None and call_bucket is not None):
            if memb_tech is not None:
                # DEPT-ALIGNED basis: this tech's slice of the department's
                # non-member completed-job denominator.
                m = mrow or {"nonMemberJobs": 0, "offered": 0, "soldOnOffer": 0}
                offered, offer_jobs = m["offered"], m["nonMemberJobs"]
                sold_on_offer = m["soldOnOffer"]
                row["membershipOfferBasis"] = "nonMemberCompletedJobs"
            else:
                offered = len(offer_bucket.get(name, ()))
                offer_jobs = len(call_bucket.get(name, ()))
                sold_on_offer = len(sold_bucket.get(name, ())) if sold_bucket is not None else 0
                row["membershipOfferBasis"] = "jobTypeProxy"
            assert offered <= offer_jobs, (
                "membershipOffered > membershipOfferJobs for %s (%d > %d) — "
                "numerator/denominator population mismatch" % (name, offered, offer_jobs))
            row["membershipOffered"] = offered
            row["membershipOfferJobs"] = offer_jobs
            row["membershipOfferRate"] = round(offered / offer_jobs * 1000) / 10 if offer_jobs else 0
            assert sold_on_offer <= offered, (
                "membershipSoldOnOffer > membershipOffered for %s (%d > %d) — "
                "a sold-with-membership job must be a subset of offered" % (name, sold_on_offer, offered))
            row["membershipSoldOnOffer"] = sold_on_offer
            row["membershipCloseOnOffer"] = round(sold_on_offer / offered * 1000) / 10 if offered else None
        else:
            row["membershipOffered"] = None
            row["membershipOfferJobs"] = None
            row["membershipOfferRate"] = None
            row["membershipSoldOnOffer"] = None
            row["membershipCloseOnOffer"] = None
            row["membershipOfferBasis"] = None
        rows.append(row)
    rows.sort(key=lambda r: -r["revenue"])
    return rows


def dept_summary(w, dept_agg, sales_dept, rev_bucket, memb=None, entity_sales=False):
    """One Overview / Department Pulse window block. DEPARTMENT-WIDE as of
    2026-08-06 (see fc_dept_rows) — `dept_agg` must come from fc_dept_rows(),
    NOT fc_tech_rows().

    revenue / avgTicket: the FC report's CompletedRevenueWithAdjustments and
    CompletedJobs, i.e. ONE population top and bottom, on a completion basis.
    The old revenue came from revenue_for_range() (invoice-date basis, no tech
    or roster filter) and read $64,801 for Aug 5 against the deck's $60,629
    while jobs came from the roster FC agg — two different populations, so the
    avg ticket was meaningless ($691 vs the true $481). revenue_for_range() is
    still the right basis for the budget/pace card, which is about cash posted,
    and it still drives `revenueBU` (the only genuine BU split available).

    memb: build_membership_dept() entry for this window, or None to fall back
    to the FC report's rounded membership proxy (YTD only)."""
    t = fc_totals(dept_agg)
    opp, converted, jobs, leads = t["opp"], t["converted"], t["jobs"], t["leads"]
    rev = t["rev"]
    sales_val = sales_dept if entity_sales else t["rev"]
    out = {
        "opps": int(opp), "converted": int(converted),
        "closeRate": round(converted / opp * 1000) / 10 if opp else 0,
        "sales": round(sales_val), "salesIsApprox": not entity_sales,
        "avgSale": round(sales_val / converted) if converted else 0,
        "revenue": round(rev), "revenueBU": {BU_NAME[k]: round(v) for k, v in rev_bucket["bu"].items()},
        "revenueInvoiced": round(rev_bucket["total"]),
        "jobs": int(jobs), "avgTicket": round(rev / jobs) if jobs else 0,
        "leadsSet": leads,
        "optionsPerOpp": round(t["oppW"] / opp, 2) if opp else 0,
    }
    if memb:
        # Real counts. membershipOpps is the NON-MEMBER completed-job
        # denominator (customers who could actually be sold), so the UI's
        # "sold / opps" subtitle and membershipConv stay one arithmetic pair.
        denom = memb["nonMemberJobs"]
        offered = memb["offered"]
        sold_on_offer = memb["soldOnOffer"]
        assert offered <= denom, (
            "dept membershipOffered > membershipOfferJobs (%d > %d) — "
            "numerator/denominator population mismatch" % (offered, denom))
        assert sold_on_offer <= offered, (
            "dept membershipSoldOnOffer > membershipOffered (%d > %d) — "
            "a sold-with-membership job must be a subset of offered" % (sold_on_offer, offered))
        out["membershipsSold"] = int(memb["sold"])
        out["membershipsSoldIsApprox"] = False
        out["membershipOpps"] = denom
        out["membershipConv"] = round(memb["sold"] / denom * 1000) / 10 if denom else 0
        out["membershipOffered"] = offered
        out["membershipOfferJobs"] = denom
        out["membershipOfferRate"] = round(offered / denom * 1000) / 10 if denom else 0
        out["membershipSoldOnOffer"] = sold_on_offer
        out["membershipCloseOnOffer"] = round(sold_on_offer / offered * 1000) / 10 if offered else None
    else:
        # YTD: a per-customer membership-coverage pass over ~12k customers is
        # not affordable (see build_membership_dept), so keep the FC report's
        # rounded Opportunity x MembershipConversionRate proxy and say so.
        membOpp, membSold = t["membOpp"], t["membSold"]
        out["membershipsSold"] = int(membSold)
        out["membershipsSoldIsApprox"] = True
        out["membershipOpps"] = int(membOpp)
        out["membershipConv"] = round(membSold / membOpp * 1000) / 10 if membOpp else 0
        out["membershipOffered"] = None
        out["membershipOfferJobs"] = None
        out["membershipOfferRate"] = None
        out["membershipSoldOnOffer"] = None
        out["membershipCloseOnOffer"] = None
    return out


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
                       {"businessUnitId": bu, "invoicedOnOrAfter": date_lower_bound(prior_monday)})
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
LIGHT_MAX_FULL_RUN_AGE_H = 25
"""Max age of the last FULL run a --light run will tolerate before promoting
itself. The full rebuild is a daily 5:35 AM PT cron, so a healthy light run
never sees more than ~24h; 25h is one hour of grace past the next scheduled
full run. Was 26h — halved the grace so a skipped 5:35 AM rebuild is caught by
~6:35 AM instead of ~7:35 AM. It cannot go below 24h without escalating every
afternoon by construction."""


def _win_num(old, w, key):
    v = ((old or {}).get(w) or {}).get(key)
    return v if isinstance(v, (int, float)) else None


def containment_pairs():
    """[(bigger, smaller)] window pairs where `bigger` really does contain
    `smaller` THIS week.

    Not a constant: in the first partial week of a month WTD_START is in the
    PREVIOUS month, so wtd is wider than mtd and mtd.revenue < wtd.revenue is
    correct arithmetic, not a bug. Asserting the pair unconditionally would
    crash the engine for the first few days of most months."""
    out = []
    for bigger, smaller in (("wtd", "today"), ("mtd", "wtd"), ("ytd", "mtd")):
        bf, bt = WINDOWS[bigger]
        sf, st_ = WINDOWS[smaller]
        if bf <= sf and bt >= st_:
            out.append((bigger, smaller))
    return out


def _light_escalation_reason(old):
    """Return a reason string if a --light run should be promoted to a full
    run, else None. Cheap: a handful of dict lookups + one timestamp parse."""
    cur_month = MONTHS[TODAY.month - 1]
    if old.get("mtdMonth") != cur_month:
        return "mtd month stale (seeded=%r, current=%r)" % (old.get("mtdMonth"), cur_month)
    monthly = old.get("monthly") or []
    if not any(m.get("month") == cur_month for m in monthly):
        return "monthly[] missing current month %r" % cur_month
    # Window containment: mtd is a strict superset of wtd, ytd of mtd. A seed
    # that violates that was built from mismatched vintages (the 2026-08-02
    # incident published a July mtd next to an August wtd). Light runs now
    # recompute mtd live so this normally self-heals in one cycle, but ytd is
    # still carried forward and only a full run can repair it.
    for bigger, smaller in containment_pairs():
        if bigger == "wtd":
            continue                      # today/wtd are always recomputed live
        for key in ("revenue", "jobs"):
            b, s = _win_num(old, bigger, key), _win_num(old, smaller, key)
            if b is None or s is None:
                return "seeded %s/%s missing %r — cannot verify window containment" % (bigger, smaller, key)
            if b < s:
                return "seeded %s.%s (%s) < %s.%s (%s) — inconsistent window vintages" % (
                    bigger, key, b, smaller, key, s)
    lfr = old.get("lastFullRun")
    if not lfr:
        return "no lastFullRun stamp found"
    try:
        last = dt.datetime.fromisoformat(lfr)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        age_h = (NOW - last).total_seconds() / 3600
        if age_h > LIGHT_MAX_FULL_RUN_AGE_H:
            return "lastFullRun stale (%.1fh old, limit %dh)" % (age_h, LIGHT_MAX_FULL_RUN_AGE_H)
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

    # LIGHT RUNS NOW COMPUTE MTD LIVE (2026-08-06).
    # Previously light runs skipped mtd/ytd entirely and republished whatever
    # the last full run left behind, so a dashboard could show MTD revenue
    # BELOW WTD revenue for most of a day. mtd is one extra FC report call plus
    # a slightly longer invoice pull — cheap. ytd is still copied forward: an
    # FC report over 7+ months and a fresh monthly[] series are what make a
    # full run 12 minutes, and light runs have ~2. _light_escalation_reason()
    # now hard-checks window containment on the SEED, so a carried-forward ytd
    # that has fallen behind mtd promotes the next light run to a full one.
    if light:
        live_windows = {w: WINDOWS[w] for w in ("today", "wtd", "mtd")}
        if TODAY.month == 1:
            # In January the YTD span IS the MTD span, so computing it live
            # costs one more (short) report call — and NOT computing it live
            # would leave a frozen ytd sitting below a growing mtd, tripping
            # the containment guard on every light run of the new year.
            live_windows["ytd"] = WINDOWS["ytd"]
    else:
        live_windows = dict(WINDOWS)

    log("FC v1 report x%d window(s)" % len(live_windows))
    fc_by_window, fc_dept_by_window = {}, {}
    for w, (frm, to) in live_windows.items():
        c, rows = fc_window(frm, to)
        fc_by_window[w] = fc_tech_rows(c, rows, roster)          # By Tech (roster)
        fc_dept_by_window[w] = fc_dept_rows(c, rows)             # Overview tiles (dept-wide)
        log("  %s: %d roster techs / %d dept techs w/ rows"
            % (w, len(fc_by_window[w]), len(fc_dept_by_window[w])))
        time.sleep(0.5)

    # Department SALES: one long sold-estimate pull, no tech join (28.7s for a
    # full YTD, measured 2026-08-06). Light runs only need back to the start of
    # the month; ytd.sales is carried forward with the rest of the ytd block.
    # min(): in the first partial week of a month WTD_START is in the PREVIOUS
    # month, and recent_sold_estimates()/the wtd tile still need those days.
    sales_span_start = min(MTD_START, WTD_START) if light else YTD_START
    log("sold estimates (dept-wide sales$ since %s)" % sales_span_start)
    dept_sold_ests = fetch_sold_estimates_dept(sales_span_start)
    sales_dept_wide = bucket_sales_dept(dept_sold_ests, live_windows)
    ests = recent_sold_estimates(dept_sold_ests)

    # Entity membership pass — today/wtd always, mtd on FULL runs only (see
    # build_membership_dept for the cost measurements).
    memb_windows = {w: WINDOWS[w] for w in (("today", "wtd") if light else ("today", "wtd", "mtd"))}
    memb_span_start = min(frm for frm, _ in memb_windows.values())

    log("membership-offer estimates (entity, since %s)" % memb_span_start)
    offer_ests = fetch_membership_offer_estimates(memb_span_start)

    # The department membership pass has to happen BEFORE the job->tech join now
    # (2026-08-06): its completed-job set is also the per-tech offer denominator,
    # so those job ids ride along in the SAME fetch_job_tech_map() call instead
    # of costing a second pull. The old fetch_tech_call_universe() job-type-proxy
    # denominator (a ~12s company-wide appointment pull) is gone — see the
    # SUPERSEDED block at section 3c.
    log("dept membership entity pass (%s)" % ", ".join(sorted(memb_windows)))
    completed_jobs = fetch_completed_dept_jobs(memb_span_start, TODAY)
    memb_cov = fetch_membership_coverage(j.get("customerId") for j in completed_jobs)
    memberships_created = fetch_memberships_created(memb_span_start, TODAY)

    # Only jobs completed inside the per-tech windows (today/wtd) need tech
    # attribution — a FULL run's mtd membership pass is department-only, and
    # feeding a month of jobs into the assignment join would cost far more than
    # the By Tech table is worth. known_jobs= hands fetch_job_tech_map the job
    # objects we already have so it only re-fetches what it has never seen.
    completed_by_id = {j["id"]: j for j in completed_jobs}
    memb_tech_job_ids = {j["id"] for j in completed_jobs
                         if (parse_ts(j.get("completedOn")) or NOW).date() >= WTD_START}
    job_ids = {e["jobId"] for e in ests if e.get("jobId")}
    job_ids |= memb_tech_job_ids
    log("  unique jobs to resolve tech for: %d (%d of them completed dept jobs "
        "already in hand)" % (len(job_ids), len(memb_tech_job_ids)))
    multi_tech = {}
    job_tech = fetch_job_tech_map(job_ids, roster, known_jobs=completed_by_id,
                                  all_techs=multi_tech)
    log("  resolved tech for %d jobs" % len(job_tech))
    sales_by_window, sales_dept_roster = bucket_sales(ests, job_tech)
    log("  wtd sales$: dept-wide $%s vs roster-attributed $%s (the gap is off-roster "
        "dept labour — the SALES tile shows the dept figure, By Tech shows the roster one)"
        % (format(round(sales_dept_wide.get("wtd", 0.0)), ","),
           format(round(sales_dept_roster.get("wtd", 0.0)), ",")))

    memb_by_window, memb_tech_by_window, memb_recon = build_membership_dept(
        memb_windows, completed_jobs, memb_cov, memberships_created, offer_ests,
        job_tech=job_tech, tech_windows=ENTITY_SALES_WINDOWS)
    for w in sorted(memb_by_window):
        log("  %s: sold %d / non-member jobs %d / offered %d"
            % (w, memb_by_window[w]["sold"], memb_by_window[w]["nonMemberJobs"],
               memb_by_window[w]["offered"]))
    # Reconciliation: the By Tech offer denominator is a PARTITION of the dept
    # one, minus the jobs run by labour that isn't on the 2A/2B roster. Logged
    # every run so the gap is visible instead of inferred.
    for w in sorted(memb_recon):
        r = memb_recon[w]
        log("  %s per-tech offer reconciliation: %d/%d dept non-member jobs attributed to a "
            "roster tech, %d off-roster/unassigned (kept in the dept tile, absent from By "
            "Tech by design); offered %d/%d" %
            (w, r["techNonMemberJobs"], r["deptNonMemberJobs"], r["unattributedJobs"],
             r["techOffered"], r["deptOffered"]))
        assert r["techNonMemberJobs"] + r["unattributedJobs"] == r["deptNonMemberJobs"], (
            "per-tech offer denominator does not partition the dept denominator for %s "
            "(%d + %d != %d)" % (w, r["techNonMemberJobs"], r["unattributedJobs"],
                                 r["deptNonMemberJobs"]))
    _multi = sum(1 for jid in memb_tech_job_ids if len(multi_tech.get(jid, ())) > 1)
    log("  %d of %d window jobs had >1 active roster tech on the first appointment "
        "(counted once, to the first — no double counting)" % (_multi, len(memb_tech_job_ids)))

    if light:
        # small invoice pull: covers MTD (revenueBU + budget-month todayCommit)
        # and WTD. Only revenueBU / the budget card read invoices now — the
        # REVENUE tile is the FC report's completion-basis figure.
        try:
            bcfg = json.load(open(os.path.join(HERE, "service_budget.json")))
            by, bm = (int(x) for x in bcfg["month"].split("-"))
            bstart = dt.date(by, bm, 1)
        except Exception:
            bstart = MTD_START
        fetch_from = min(WTD_START, MTD_START, bstart if TODAY >= bstart else MTD_START)
        log("invoices (light: revenue$ from %s)" % fetch_from)
        invs = []
        for bu in DEPT_BUS:
            invs += paged("/accounting/v2/tenant/{tenant}/invoices",
                           {"businessUnitId": bu, "invoicedOnOrAfter": date_lower_bound(fetch_from)})
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
        monthly = monthly_series(invs)

    log("board counts (today)")
    board = board_counts([j for j in completed_jobs
                          if (parse_ts(j.get("completedOn")) or NOW).date() == TODAY])

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
    weekly = weekly_series(roster, invs, fc_dept_by_window["wtd"], sales_dept_wide.get("wtd", 0.0),
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
    _MEMB_KEYS = ("membershipOffered", "membershipOfferJobs", "membershipOfferRate",
                  "membershipSoldOnOffer", "membershipCloseOnOffer")
    _MEMB_ROW_KEYS = _MEMB_KEYS + ("membershipOfferBasis",)
    for w in WINDOWS:
        if w not in live_windows:
            # ytd on a light run — carry the last full run's block forward.
            out[w] = old.get(w, {})
            for k in _MEMB_KEYS:
                out[w].setdefault(k, None)
            out[w].setdefault("membershipsSoldIsApprox", True)
            out[w].setdefault("revenueInvoiced", out[w].get("revenue"))
            out[w].setdefault("optionsPerOpp", 0)
            out["techs"][w] = old.get("techs", {}).get(w, [])
            for row in out["techs"][w]:
                for k in _MEMB_ROW_KEYS:
                    row.setdefault(k, None)
            continue
        out[w] = dept_summary(w, fc_dept_by_window[w], sales_dept_wide.get(w, 0.0),
                              rev_by_window[w], memb=memb_by_window.get(w),
                              entity_sales=True)
        if light and w == "mtd" and not memb_by_window.get(w):
            # Light runs skip the mtd membership entity pass (too slow — see
            # build_membership_dept). Carry the last full run's REAL mtd
            # membership counts rather than silently swapping in the FC
            # report's rounded proxy, which would make the tile jump between
            # two different definitions every 10 minutes.
            prior = (old or {}).get("mtd") or {}
            if prior.get("membershipsSoldIsApprox") is False:
                for k in ("membershipsSold", "membershipsSoldIsApprox", "membershipOpps",
                          "membershipConv") + _MEMB_KEYS:
                    out[w][k] = prior.get(k)
                out[w]["membershipsAsOf"] = prior.get("membershipsAsOf") or (old or {}).get("lastFullRun")
        # By Tech / techDaily / alerts / coaching stay ROSTER-scoped. The offer
        # columns are the dept denominator PARTITIONED by tech (memb_tech), so
        # the table and the tile above it are the same question — today/wtd
        # only, exactly as before; mtd/ytd per-tech offer fields stay null
        # rather than flapping between definitions on light vs full runs.
        sb = sales_by_window.get(w, {})
        mt = memb_tech_by_window.get(w) if w in ENTITY_SALES_WINDOWS else None
        out["techs"][w] = tech_rows_for_window(w, fc_by_window[w], sb, roster, memb_tech=mt)
    if not light:
        for w in ("today", "wtd", "mtd"):
            if (out[w].get("membershipsSoldIsApprox") is False):
                out[w]["membershipsAsOf"] = NOW.isoformat()

    # Containment invariant — a wider window is a strict superset of a
    # narrower one, so it can never hold less revenue or fewer jobs. Publishing
    # a violation is exactly the 2026-08-02 failure mode (July's mtd sitting
    # under August's wtd).
    #   both windows computed live this run -> HARD FAIL, the data is wrong.
    #   one carried forward (ytd on a non-January light run) -> loud warning;
    #     _light_escalation_reason() sees it in the seed and promotes the next
    #     light run to a full one, so it self-heals within one cycle instead of
    #     taking the dashboard down.
    for bigger, smaller in containment_pairs():
        for key in ("revenue", "jobs"):
            b, s = out[bigger].get(key), out[smaller].get(key)
            if not (isinstance(b, (int, float)) and isinstance(s, (int, float))) or b >= s:
                continue
            msg = ("%s.%s (%s) < %s.%s (%s) — the wider window is a strict superset "
                   "and cannot be smaller" % (bigger, key, b, smaller, key, s))
            if bigger in live_windows and smaller in live_windows:
                raise RuntimeError("REFUSING TO PUBLISH: " + msg +
                                   ". Both windows were computed live this run, so this is a "
                                   "real aggregation bug, not stale carry-forward.")
            log("WARNING: %s (carried-forward window) — the next light run will escalate to "
                "a full rebuild and repair it" % msg)

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
    for w in sorted(memb_recon):
        r = memb_recon[w]
        print("per-TECH offer denominator (%s): %d of %d dept non-member jobs attributed to a "
              "roster tech, %d off-roster/unassigned" %
              (w, r["techNonMemberJobs"], r["deptNonMemberJobs"], r["unattributedJobs"]))
    for w in ("today", "wtd", "mtd"):
        b = out[w]
        print("%-5s dept membership: sold=%s / non-member jobs=%s = %s%% | offered=%s (%s%%) | "
              "closeOnOffer=%s%s" % (
                  w, b.get("membershipsSold"), b.get("membershipOpps"), b.get("membershipConv"),
                  b.get("membershipOffered"), b.get("membershipOfferRate"),
                  b.get("membershipCloseOnOffer"),
                  "  [FC approx]" if b.get("membershipsSoldIsApprox") else ""))


if __name__ == "__main__":
    if "--week-review" in sys.argv:
        week_review_main()
    else:
        main()
