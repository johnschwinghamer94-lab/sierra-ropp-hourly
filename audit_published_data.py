#!/usr/bin/env python3
"""
audit_published_data.py -- PUBLISHED-DATA AUDIT for the sierra-ropp-dashboard.

Companion to audit_dashboard.py. Where that script re-aggregates the day's
ServiceTitan reports and diffs the result against index.html, this one needs no
source reports and no credentials: it audits the *published payload itself* --
every JSON feed plus the data blocks embedded in index.html -- for staleness,
internal arithmetic errors, roll-up drift between blocks that must agree, and
name/roster inconsistencies.

That makes it runnable anywhere (CI, a laptop, a cloud box) against nothing but
a checkout of the public dashboard repo, and it catches the failure modes the
report-recount audit structurally cannot see: a feed that stopped updating, a
block that disagrees with another block, one rep split across two spellings.

Usage:
    python audit_published_data.py --site /path/to/sierra-ropp-dashboard
    python audit_published_data.py --site ./site --date 2026-08-03

Exit code 0 if no FAILs, 1 otherwise. WARN/INFO never fail the run.
"""
import argparse, json, os, re, sys, datetime as dt
from collections import Counter, defaultdict

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

ap = argparse.ArgumentParser()
ap.add_argument("--site", default=None, help="checkout of the public dashboard repo")
ap.add_argument("--date", default=None, help="treat this as today (YYYY-MM-DD)")
ap.add_argument("--stale-hours", type=float, default=3.0,
                help="live feeds older than this raise a WARN (default 3)")
ap.add_argument("--quiet", action="store_true", help="findings only, skip the detail sections")
A = ap.parse_args()

SITE = A.site or next((p for p in (
    os.path.join(os.getcwd(), "site"),
    r"C:\Users\johns\sierra-ropp-dashboard",
    "/workspace/sierra-ropp-dashboard") if os.path.isdir(p)), None)
if not SITE or not os.path.isdir(SITE):
    print("AUDIT: could not find the dashboard checkout (pass --site)")
    sys.exit(2)

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:                                   # no tzdata — fall back to PDT
    PT = dt.timezone(dt.timedelta(hours=-7))

# Every stamp in the payload is Vegas/PT: the ISO fields carry a PT offset and the
# *Ms epochs were taken on PT boxes. Compare everything as naive PT wall-clock so a
# UTC runner does not read a fresh feed as 7 hours stale.
def pt(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, PT).replace(tzinfo=None)


TODAY = dt.date.fromisoformat(A.date) if A.date else dt.date.today()
NOW = dt.datetime.now(PT).replace(tzinfo=None)
if A.date:                      # deterministic replay: anchor "now" to the newest feed stamp
    NOW = dt.datetime.combine(TODAY, dt.time(0, 0))

FINDINGS = []


def add(sev, area, txt):
    FINDINGS.append((sev, area, txt))


def say(*a):
    if not A.quiet:
        print(*a)


def sect(title):
    say("\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78)


def load(name, required=True):
    p = os.path.join(SITE, name)
    if not os.path.exists(p):
        if required:
            add("FAIL", "missing", "%s is not published" % name)
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as ex:
        add("FAIL", "missing", "%s is not valid JSON: %s" % (name, ex))
        return None


HTML = open(os.path.join(SITE, "index.html"), encoding="utf-8").read()


def block(name, html=HTML):
    """Pull `const NAME = {...}` / `[...]` out of index.html as real JSON."""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*", html)
    if not m:
        return None
    i = m.end()
    while i < len(html) and html[i] not in "{[":
        i += 1
    op = html[i]
    cl = "}" if op == "{" else "]"
    depth = 0
    k = i
    instr = esc = False
    while k < len(html):
        c = html[k]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == op:
            depth += 1
        elif c == cl:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[i:k + 1])
                except Exception as ex:
                    add("FAIL", "missing", "%s in index.html is not parseable: %s" % (name, ex))
                    return None
        k += 1
    return None


def near(a, b, tol=0.06):
    return abs((a or 0) - (b or 0)) <= tol


def norm_name(s):
    """Canonical tech name: underscores and runs of whitespace collapse to one space."""
    return re.sub(r"[\s_]+", " ", s or "").strip()


# ═══════════════════════════════════════════════════ 1. freshness
sect("1. FRESHNESS")
LIVE = [("hourly.json", "ROPP hour-by-hour"), ("livefeed.json", "SILO live feed"),
        ("scorecards.json", "ROPP scorecards"), ("servicefeed.json", "Service live feed"),
        ("servicecards.json", "Service scorecards")]
stamps = []
for f, lbl in LIVE:
    d = load(f)
    if d and d.get("generatedMs"):
        stamps.append(pt(d["generatedMs"]))
sd = load("servicedata.json")
if sd:
    stamps.append(dt.datetime.fromisoformat(sd["updated"][:19]))
if A.date and stamps:
    NOW = max(stamps)          # replay: newest publish is "now"
say("  reference time: %s" % NOW.strftime("%Y-%m-%d %H:%M"))

for f, lbl in LIVE:
    d = load(f)
    if not d:
        continue
    h = (NOW - pt(d["generatedMs"])).total_seconds() / 3600
    say("  %-22s %-22s %6.2f h old" % (lbl, f, h))
    if h > A.stale_hours:
        add("WARN", "freshness", "%s is %.1fh stale" % (f, h))
    if d.get("date") and d["date"] != str(TODAY):
        add("FAIL", "freshness", "%s carries date=%s but today is %s" % (f, d["date"], TODAY))

if sd:
    for key, lim, lbl in (("updated", 1.0, "servicedata publish"),
                          ("lastFullRun", 26.0, "servicedata FULL rebuild")):
        t = dt.datetime.fromisoformat(sd[key][:19])
        h = (NOW - t).total_seconds() / 3600
        say("  %-22s %-22s %6.2f h old  (%s)" % (lbl, key, h, sd[key][:19]))
        if h > lim:
            add("WARN", "freshness", "servicedata %s is %.1fh old (limit %.0fh)" % (key, h, lim))
    if sd.get("mtdMonth") != MONTHS[TODAY.month - 1]:
        add("FAIL", "freshness", "servicedata.mtdMonth=%r but the current month is %r — the "
            "MTD panel is showing the wrong month" % (sd.get("mtdMonth"), MONTHS[TODAY.month - 1]))
    if not any(m.get("month") == MONTHS[TODAY.month - 1] for m in sd.get("monthly", [])):
        add("FAIL", "freshness", "servicedata.monthly[] has no entry for the current month")

cj = load("coaching.json")
if cj:
    h = (NOW - dt.datetime.fromisoformat(cj["generated"][:19])).total_seconds() / 3600
    say("  %-22s %-22s %6.2f h old  (covers %s)" % ("coaching", "coaching.json", h, cj.get("date")))
    if h > 30:
        add("WARN", "freshness", "coaching.json is %.1fh stale" % h)

# week reviews: the week that ended most recently must be present
for f, lbl in (("weekreview.json", "Service week review"),
               ("weekreview_silo.json", "ROPP/SILO week review")):
    d = load(f)
    if not d:
        continue
    weeks = sorted(d.get("weeks") or {})
    last_monday = TODAY - dt.timedelta(days=TODAY.weekday())
    due = (last_monday - dt.timedelta(days=7)).isoformat()   # week that ended yesterday-ish
    say("  %-22s %-22s latest=%s  weeks=%s" % (lbl, f, d.get("latest"), weeks))
    if due not in weeks:
        add("FAIL", "freshness", "%s is missing the week beginning %s (the most recently "
            "completed week); it has %s" % (f, due, weeks or "nothing"))
    else:
        e = d["weeks"][due]
        if e.get("audit") == "prelim" and TODAY.weekday() == 0 and NOW.hour >= 6:
            add("WARN", "freshness", "%s week %s is still 'prelim' past Monday 6am — the "
                "Monday finalize pass did not run" % (f, due))

# a feed the site fetches but nothing regenerates
for orphan in ("data.json",):
    p = os.path.join(SITE, orphan)
    if not os.path.exists(p):
        continue
    referenced = any(re.search(r"[/\"']" + re.escape(orphan), open(os.path.join(SITE, h),
                     encoding="utf-8", errors="ignore").read())
                     for h in os.listdir(SITE) if h.endswith((".html", ".js")))
    if not referenced:
        add("WARN", "orphan", "%s is published but no page fetches it — it is a stale artifact "
            "served from the public repo" % orphan)

# ═══════════════════════════════════════════════════ 2. index.html blocks
sect("2. index.html DATA BLOCKS — internal arithmetic")
I = block("INITIAL_DATA") or {}
counts = Counter()
for n, d in I.items():
    for per in ("ytd", "mtd"):
        p = d.get(per, {})
        c, t, r = p.get("calls", 0), p.get("tgls", 0), p.get("revenue", 0)
        if c < 0 or t < 0 or r < 0:
            add("FAIL", "math", "%s.%s has a negative value" % (n, per))
        exp = round(t / c * 100, 1) if c else 0.0
        if not near(exp, p.get("rate", 0)):
            counts["rate"] += 1
            add("WARN", "math", "%s.%s.rate=%s but tgls/calls=%s" % (n, per, p.get("rate"), exp))
        for a, b, lbl in (("svc_calls", "maint_calls", "calls"), ("svc_tgls", "maint_tgls", "tgls")):
            s = p.get(a, 0) + p.get(b, 0)
            if s != p.get(lbl, 0):
                counts["split"] += 1
                add("WARN", "math", "%s.%s: %s+%s=%d != %s=%d" % (n, per, a, b, s, lbl, p.get(lbl, 0)))
        if per == "mtd":
            for fld in ("calls", "tgls", "revenue"):
                if p.get(fld, 0) > d["ytd"].get(fld, 0):
                    add("FAIL", "math", "%s: mtd.%s exceeds ytd.%s" % (n, fld, fld))
    mon = d.get("monthly", {})
    if sum(v.get("calls", 0) for v in mon.values()) != d["ytd"]["calls"] or \
       sum(v.get("tgls", 0) for v in mon.values()) != d["ytd"]["tgls"]:
        counts["monthly"] += 1
        add("FAIL", "math", "%s: monthly[] does not sum to ytd" % n)
    cur = mon.get(MONTHS[TODAY.month - 1], {})
    if cur.get("calls", 0) != d["mtd"]["calls"] or cur.get("tgls", 0) != d["mtd"]["tgls"]:
        counts["mtd"] += 1
        add("FAIL", "math", "%s: monthly[%s] != mtd" % (n, MONTHS[TODAY.month - 1]))
say("  INITIAL_DATA: %d techs   rate=%d  monthly-sum=%d  mtd-tie=%d  svc/maint-split=%d errors"
    % (len(I), counts["rate"], counts["monthly"], counts["mtd"], counts["split"]))

# a TGL with no originating call is an attribution leak
leak = [(n, d["ytd"]["calls"], d["ytd"]["tgls"], d["ytd"]["revenue"])
        for n, d in I.items() if d["ytd"]["tgls"] > d["ytd"]["calls"]]
say("  techs whose YTD TGLs exceed their YTD calls: %d" % len(leak))
for n, c, t, r in leak:
    say("    %-30s %d calls / %d tgls   $%s" % (n, c, t, format(r, ",")))
    add("WARN", "quality", "%s: %d TGLs credited on %d calls ($%s revenue) — the call side of "
        "the attribution is missing" % (n, t, c, format(r, ",")))

# ═══════════════════════════════════════════════════ 3. block-to-block roll-ups
sect("3. ROLL-UPS — blocks that must agree with each other")
DP = block("DEPT_PACE_DATA") or {}
tc = sum(d["ytd"]["calls"] for d in I.values())
tt = sum(d["ytd"]["tgls"] for d in I.values())
tr = sum(d["ytd"]["revenue"] for d in I.values())
say("  per-tech YTD sum      %d calls  %d tgls  $%s" % (tc, tt, format(tr, ",")))
say("  DEPT_PACE_DATA        %d calls  %d tgls  $%s"
    % (DP.get("total_calls", 0), DP.get("total_tgls", 0), format(DP.get("ytd_revenue", 0), ",")))
for lbl, a, b in (("total_calls", tc, DP.get("total_calls")),
                  ("total_tgls", tt, DP.get("total_tgls")),
                  ("ytd_revenue", tr, DP.get("ytd_revenue"))):
    if a != b:
        add("FAIL", "rollup", "DEPT_PACE_DATA.%s=%s but the per-tech rows sum to %s (Δ%+d)"
            % (lbl, b, a, (b or 0) - a))
if DP.get("total_calls"):
    e = round(DP["total_tgls"] / DP["total_calls"] * 100, 1)
    if not near(e, DP.get("conv_rate", 0)):
        add("WARN", "rollup", "DEPT_PACE_DATA.conv_rate=%s but tgls/calls=%s" % (DP.get("conv_rate"), e))
if DP.get("days_elapsed", 0) + DP.get("days_remaining", 0) != DP.get("days_total", 0):
    add("WARN", "rollup", "DEPT_PACE_DATA day counts do not add up to days_total")
if DP.get("ytd_revenue", 0) - DP.get("expected_pace", 0) != DP.get("ahead", 0):
    add("WARN", "rollup", "DEPT_PACE_DATA.ahead != ytd_revenue - expected_pace")
if DP.get("silo_rev", 0) + DP.get("other_rev", 0) != DP.get("total_revenue", 0):
    add("WARN", "rollup", "DEPT_PACE_DATA silo_rev + other_rev != total_revenue")

MD = block("MONTHLY_DETAIL") or {}
bad = 0
for mo, techs in MD.items():
    for n, v in techs.items():
        im = I.get(n, {}).get("monthly", {}).get(mo, {})
        if v.get("calls", 0) != im.get("calls", 0) or v.get("total_tgls", 0) != im.get("tgls", 0):
            bad += 1
            add("FAIL", "rollup", "MONTHLY_DETAIL[%s][%s] disagrees with INITIAL_DATA.monthly" % (mo, n))
md_rev = sum(v.get("total_rev", 0) for t in MD.values() for v in t.values())
say("  MONTHLY_DETAIL: %d months, %d cell disagreements, revenue total $%s"
    % (len(MD), bad, format(md_rev, ",")))
if md_rev != DP.get("ytd_revenue"):
    d = DP.get("ytd_revenue", 0) - md_rev
    sev = "INFO" if abs(d) <= max(10, len(MD)) else "FAIL"
    add(sev, "rollup", "MONTHLY_DETAIL revenue sums to $%s vs DEPT_PACE_DATA $%s (Δ%+d — %s)"
        % (format(md_rev, ","), format(DP.get("ytd_revenue", 0), ","), d,
           "per-month rounding" if sev == "INFO" else "real drift"))

P = block("PACE_DATA") or {}
pm = pr_ = 0
for n, p in P.items():
    if n in I and p.get("ytd_revenue") != I[n]["ytd"]["revenue"]:
        pm += 1
        add("FAIL", "rollup", "PACE_DATA[%s].ytd_revenue != INITIAL_DATA revenue" % n)
    s = sum(m.get("revenue", 0) for m in p.get("monthly", []))
    if s != p.get("ytd_revenue"):
        pr_ += 1
        add("INFO", "rollup", "PACE_DATA[%s] monthly revenue sums to %d vs ytd_revenue %d (Δ%+d — "
            "per-month rounding)" % (n, s, p.get("ytd_revenue", 0), p.get("ytd_revenue", 0) - s))
say("  PACE_DATA: %d techs, %d revenue disagreements, %d monthly-sum rounding gaps"
    % (len(P), pm, pr_))
if set(I) - set(P):
    add("WARN", "roster", "techs in INITIAL_DATA but missing from PACE_DATA: %s" % sorted(set(I) - set(P)))

for nm, key in (("ALLTEAMS_DATA", "ytd_calls"), ("SILO_ONLY_DATA", "ytd_calls")):
    b = block(nm)
    rows = b.get("techs", []) if isinstance(b, dict) else (b or [])
    bad = 0
    for r in rows:
        n = r.get("name")
        if n not in I:
            add("WARN", "roster", "%s carries '%s', absent from INITIAL_DATA" % (nm, n))
            continue
        if r.get("ytd_calls") != I[n]["ytd"]["calls"] or r.get("ytd_rev") != I[n]["ytd"]["revenue"]:
            bad += 1
            add("FAIL", "rollup", "%s[%s] disagrees with INITIAL_DATA" % (nm, n))
    say("  %-16s %d rows, %d disagreements with INITIAL_DATA" % (nm, len(rows), bad))

# ═══════════════════════════════════════════════════ 4. CA close rate
sect("4. CLOSE_RATE_DATA — CA close rate")
C = block("CLOSE_RATE_DATA") or {}
T = C.get("totals", {})
a = {r["name"] for r in C.get("team_a", [])}
b = {r["name"] for r in C.get("team_b", [])}
c = {r["name"] for r in C.get("combined", [])}
say("  team_a=%d  team_b=%d  combined=%d" % (len(a), len(b), len(c)))
if a & b:
    add("FAIL", "roster", "reps on BOTH teams: %s" % sorted(a & b))
if c - (a | b):
    for n in sorted(c - (a | b)):
        row = next(r for r in C["combined"] if r["name"] == n)["ytd"]
        add("FAIL", "roster", "'%s' is in the combined close-rate table but on neither team_a nor "
            "team_b — %d ran / %d sold / $%s is invisible in both team views and makes "
            "team_a+team_b != combined" % (n, row["ran"], row["sold"], format(row["sales"], ",")))
if (a | b) - c:
    add("FAIL", "roster", "on a team but missing from combined: %s" % sorted((a | b) - c))

for grp in ("team_a", "team_b", "combined"):
    for row in C.get(grp, []):
        for per in ("ytd", "mtd"):
            g = row.get(per, {})
            if g.get("sold", 0) > g.get("ran", 0):
                add("FAIL", "math", "%s/%s.%s: sold > ran" % (grp, row["name"], per))
            if g.get("ran") and not near(round(g["sold"] / g["ran"] * 100, 1), g.get("close_rate", 0)):
                add("WARN", "math", "%s/%s.%s.close_rate is not sold/ran" % (grp, row["name"], per))
            if g.get("ropps") and not near(round(g["actual"] / g["ropps"] * 100, 1), g.get("tgl_pct", 0)):
                add("WARN", "math", "%s/%s.%s.tgl_pct is not actual/ropps" % (grp, row["name"], per))
            if g.get("tgls", 0) - g.get("canceled", 0) != g.get("actual", 0):
                add("WARN", "math", "%s/%s.%s: tgls - canceled != actual" % (grp, row["name"], per))

for per in ("ytd", "mtd"):
    for fld in ("ropps", "tgls", "canceled", "actual", "ran", "sold", "sales"):
        x = T.get(per, {}).get("team_a", {}).get(fld, 0) + T.get(per, {}).get("team_b", {}).get(fld, 0)
        y = T.get(per, {}).get("combined", {}).get(fld, 0)
        if x != y:
            add("FAIL", "rollup", "CLOSE_RATE totals.%s.%s: team_a+team_b=%s != combined=%s (Δ%+d)"
                % (per, fld, x, y, y - x))
    for grp in ("team_a", "team_b", "combined"):
        for fld in ("ran", "sold", "sales", "ropps", "tgls"):
            s = sum(r.get(per, {}).get(fld, 0) for r in C.get(grp, []))
            if s != T.get(per, {}).get(grp, {}).get(fld, 0):
                add("FAIL", "rollup", "CLOSE_RATE totals.%s.%s.%s=%s but its rows sum to %s"
                    % (per, grp, fld, T[per][grp][fld], s))
for grp in ("team_a", "team_b", "combined"):
    ms = C.get("monthly", {}).get(grp, [])
    for fld in ("ran", "sold", "sales"):
        s = sum(m.get(fld, 0) for m in ms)
        tot = T.get("ytd", {}).get(grp, {}).get(fld, 0)
        if s != tot:
            # money is rounded per month before it is summed, so a few dollars of
            # drift over 8 months is arithmetic, not a data problem
            sev = "INFO" if fld == "sales" and abs(s - tot) <= len(ms) else "FAIL"
            add(sev, "rollup", "CLOSE_RATE monthly.%s.%s sums to %s != ytd total %s (Δ%+d%s)"
                % (grp, fld, s, tot, tot - s, " — per-month rounding" if sev == "INFO" else ""))
if T:
    y = T["ytd"]["combined"]
    say("  YTD combined %d/%d = %s%%  $%s" % (y["sold"], y["ran"], y["close_rate"], format(y["sales"], ",")))

# ═══════════════════════════════════════════════════ 5. cancels / same-day
sect("5. CANCEL_DATA / SAMEDAY_DATA")
CA = block("CANCEL_DATA") or {}
SA = block("SAMEDAY_DATA") or {}
cur = MONTHS[TODAY.month - 1]
for nm, blk in (("CANCEL_DATA", CA), ("SAMEDAY_DATA", SA)):
    mos = blk.get("months", [])
    say("  %-14s months=%s weeks=%s" % (nm, mos, blk.get("weeks")))
    if mos and mos[-1] != cur:
        add("FAIL", "freshness", "%s months end at %s but it is %s" % (nm, mos[-1], cur))
for n, v in (SA.get("ytd") or {}).items():
    if v.get("flipped", 0) + v.get("nextday", 0) > v.get("total", 0):
        add("FAIL", "math", "SAMEDAY %s: flipped+nextday exceeds total" % n)
    if n not in I:
        add("WARN", "roster", "SAMEDAY_DATA carries '%s', absent from INITIAL_DATA" % n)
for n, v in (CA.get("ytd") or {}).items():
    if isinstance(v, dict) and v.get("cancelled", 0) > v.get("scheduled", 0):
        # 'scheduled' is counted on the scheduled date, 'cancelled' on the cancel date,
        # so a job scheduled last year and killed this year lands in one and not the
        # other. Legitimate -- but any per-tech cancel RATE built from this pair is
        # undefined (scheduled=0) or over 100%.
        add("WARN", "quality", "CANCEL_DATA %s: %d cancelled against %d scheduled — the two "
            "sides are counted on different dates, so a cancel rate from this pair is "
            "meaningless for this tech" % (n, v.get("cancelled", 0), v.get("scheduled", 0)))
tot = sum(v["total"] for v in (SA.get("ytd") or {}).values())
fl = sum(v["flipped"] for v in (SA.get("ytd") or {}).values())
if tot:
    say("  same-day dept: %d/%d flipped = %.1f%%" % (fl, tot, fl / tot * 100))
cy = CA.get("ytd") or {}
ccomb = sum(cy.get(n, {}).get("cancelled", 0) for n in c)
if T and ccomb != T["ytd"]["combined"]["canceled"]:
    add("WARN", "crosscheck", "CANCEL_DATA over the CA roster = %d but CLOSE_RATE ytd canceled = %d"
        % (ccomb, T["ytd"]["combined"]["canceled"]))

# ═══════════════════════════════════════════════════ 6. hour-by-hour / live
sect("6. HOUR-BY-HOUR + LIVE FEEDS")
H = load("hourly.json")
HS = load("hourly_state.json")
LF = load("livefeed.json")
if H:
    hrs = H.get("hours", [])
    say("  hourly today: %d calls / %d tgls, %d buckets" % (H["today"]["calls"], H["today"]["tgls"], len(hrs)))
    # buckets are cumulative with d* deltas — must be non-decreasing and end at today
    for i in range(1, len(hrs)):
        if hrs[i]["calls"] < hrs[i - 1]["calls"] or hrs[i]["tgls"] < hrs[i - 1]["tgls"]:
            add("FAIL", "math", "hourly.json cumulative buckets go backwards at hour %s" % hrs[i]["hour"])
        if hrs[i]["calls"] - hrs[i - 1]["calls"] != hrs[i].get("dcalls", 0):
            add("WARN", "math", "hourly.json hour %s: dcalls does not match the cumulative step"
                % hrs[i]["hour"])
    if hrs and (hrs[-1]["calls"] != H["today"]["calls"] or hrs[-1]["tgls"] != H["today"]["tgls"]):
        add("FAIL", "math", "hourly.json last bucket (%dc/%dt) != today (%dc/%dt)"
            % (hrs[-1]["calls"], hrs[-1]["tgls"], H["today"]["calls"], H["today"]["tgls"]))
    if H["today"]["calls"] and not near(round(H["today"]["tgls"] / H["today"]["calls"] * 100, 1),
                                        H["today"].get("rate", 0), 0.2):
        add("WARN", "math", "hourly.json today.rate is not tgls/calls")
    st = sum(t["calls"] for t in H["today"].get("techs", []))
    if st != H["today"]["calls"]:
        add("FAIL", "math", "hourly.json today.techs[] calls sum to %d != today.calls %d"
            % (st, H["today"]["calls"]))
if H and HS and H.get("date") != HS.get("date"):
    add("FAIL", "freshness", "hourly.json and hourly_state.json are on different dates")
if H and HS:
    for h in H.get("hours", []):
        s = HS.get("hours", {}).get(h["hour"])
        if s and (s["calls"] != h["calls"] or s["tgls"] != h["tgls"]):
            add("FAIL", "crosscheck", "hourly_state hour %s disagrees with hourly.json" % h["hour"])
if LF:
    K = LF.get("kpis", {})
    say("  livefeed: %d jobs, %d tgls, %d feed rows; kpis.jobs=%s tglLeads=%s"
        % (len(LF.get("jobs", [])), len(LF.get("tgls", [])), len(LF.get("feed", [])),
           K.get("jobs"), K.get("tglLeads")))
    if K.get("jobs") != len(LF.get("jobs", [])):
        add("WARN", "math", "livefeed kpis.jobs=%s but jobs[] has %d rows" % (K.get("jobs"), len(LF.get("jobs", []))))
    if K.get("tglLeads") != len(LF.get("tgls", [])):
        add("WARN", "math", "livefeed kpis.tglLeads=%s but tgls[] has %d rows"
            % (K.get("tglLeads"), len(LF.get("tgls", []))))
    if H and H["today"]["tgls"] != K.get("tglLeads"):
        add("WARN", "crosscheck", "hourly.json today.tgls=%d but livefeed kpis.tglLeads=%s — same "
            "day, same metric" % (H["today"]["tgls"], K.get("tglLeads")))

# ═══════════════════════════════════════════════════ 7. service department
sect("7. SERVICE DEPARTMENT")
if sd:
    for per in ("today", "wtd", "mtd", "ytd"):
        p = sd.get(per, {})
        say("  %-6s opps=%-6s conv=%-6s cr=%-6s rev=$%-12s jobs=%-6s avgTkt=$%s"
            % (per, p.get("opps"), p.get("converted"), p.get("closeRate"),
               format(round(p.get("revenue", 0)), ","), p.get("jobs"),
               format(round(p.get("avgTicket", 0)), ",")))
        if p.get("converted", 0) > p.get("opps", 0):
            add("FAIL", "math", "servicedata.%s: converted exceeds opps" % per)
        if p.get("opps") and not near(round(p["converted"] / p["opps"] * 100, 1), p.get("closeRate", 0), 0.2):
            add("WARN", "math", "servicedata.%s.closeRate is not converted/opps" % per)
        if p.get("jobs") and abs(round(p["revenue"] / p["jobs"]) - round(p.get("avgTicket", 0))) > 1:
            add("WARN", "math", "servicedata.%s.avgTicket is not revenue/jobs" % per)
    for x, y in (("today", "wtd"), ("wtd", "mtd"), ("mtd", "ytd")):
        for fld in ("opps", "converted", "jobs", "revenue", "sales"):
            if sd.get(x, {}).get(fld, 0) > sd.get(y, {}).get(fld, 0) + 0.5:
                add("FAIL", "math", "servicedata: %s.%s exceeds %s.%s" % (x, fld, y, fld))
    mrev = sum(m.get("revenue", 0) for m in sd.get("monthly", []))
    if abs(mrev - sd.get("ytd", {}).get("revenue", 0)) > len(sd.get("monthly", [])):
        add("WARN", "rollup", "servicedata monthly[] revenue sums to $%s != ytd $%s"
            % (format(round(mrev), ","), format(round(sd["ytd"]["revenue"]), ",")))
    B = sd.get("budget") or {}
    if B:
        say("  budget %s: target $%s over %d working days, month-so-far $%s, todayCommit $%s"
            % (B.get("month"), format(B.get("revenueTarget", 0), ","), B.get("workingDays", 0),
               format(B.get("monthActualSoFar", 0), ","), format(B.get("todayCommit", 0), ",")))
        if B.get("month") != TODAY.strftime("%Y-%m"):
            add("FAIL", "freshness", "servicedata.budget.month=%s but it is %s — service_budget.json "
                "has not been rolled to the current month" % (B.get("month"), TODAY.strftime("%Y-%m")))
        if B.get("workingDays") and abs(round(B["revenueTarget"] / B["workingDays"])
                                        - round(B.get("dailyRevTarget", 0))) > 1:
            add("WARN", "math", "budget.dailyRevTarget != revenueTarget/workingDays")
        ma, mr = B.get("monthActualSoFar", 0), sd.get("mtd", {}).get("revenue", 0)
        if ma and mr and abs(ma - mr) > max(500, 0.02 * mr):
            add("WARN", "crosscheck", "two figures for month-to-date revenue on the same page "
                "disagree: budget.monthActualSoFar=$%s vs mtd.revenue=$%s (Δ%+d, %.0f%%)"
                % (format(round(ma), ","), format(round(mr), ","), round(ma - mr),
                   abs(ma - mr) / mr * 100))
    days = (sd.get("techDaily") or {}).get("_days", [])
    if days and days[-1] != str(TODAY):
        add("WARN", "freshness", "servicedata.techDaily runs only through %s" % days[-1])

SH = load("servicedata_history.json", required=False)
if SH and sd:
    ds = sorted(SH)
    say("  history: %s" % ds)
    # mtd/ytd only move on a FULL run; if they sit still across day boundaries the
    # nightly full rebuild is not landing and the MTD panel goes stale (or shows last month)
    frozen = []
    for i in range(1, len(ds)):
        a_, b_ = SH[ds[i - 1]], SH[ds[i]]
        if a_.get("ytd", {}).get("revenue") == b_.get("ytd", {}).get("revenue") and \
           a_.get("mtd", {}).get("revenue") == b_.get("mtd", {}).get("revenue"):
            frozen.append(ds[i])
        if b_.get("ytd", {}).get("revenue", 0) < a_.get("ytd", {}).get("revenue", 0) - 1:
            add("FAIL", "math", "servicedata YTD revenue went DOWN %s -> %s" % (ds[i - 1], ds[i]))
    if frozen:
        add("FAIL", "freshness", "servicedata mtd/ytd did not move on %s — the nightly FULL "
            "rebuild did not land on %s, so the MTD/YTD panels republished the previous "
            "figures (in a new month that means last month's MTD)" % (", ".join(frozen), " / ".join(frozen)))
    for d_ in ds:
        e = SH[d_]
        if e.get("mtd", {}).get("revenue", 0) > 0 and e.get("today", {}).get("revenue", 0) > 0:
            pass
    say("  ytd revenue by day: %s" % ", ".join("%s=$%s" % (d_, format(round(SH[d_]["ytd"]["revenue"]), ","))
                                               for d_ in ds))

SF = load("servicefeed.json")
if SF and SF.get("kpis", {}).get("jobsToday") is not None and \
        SF["kpis"]["jobsToday"] != len(SF.get("jobs", [])):
    add("WARN", "math", "servicefeed kpis.jobsToday=%s but jobs[] has %d rows"
        % (SF["kpis"]["jobsToday"], len(SF.get("jobs", []))))

# ═══════════════════════════════════════════════════ 8. names / rosters
sect("8. NAME NORMALIZATION + ROSTERS")
SC = load("scorecards.json")
if SC:
    raw = Counter(x.get("tech") for x in SC.get("cards", []) if x.get("tech"))
    grouped = defaultdict(list)
    for k, v in raw.items():
        grouped[norm_name(k)].append((k, v))
    say("  scorecards.json: %d cards, %d raw spellings, %d real techs" % (len(SC.get("cards", [])), len(raw), len(grouped)))
    for canon, variants in sorted(grouped.items()):
        if len(variants) > 1:
            say("    %-22s <- %s" % (canon, ", ".join("%r x%d" % v for v in variants)))
            add("FAIL", "names", "scorecards.json splits '%s' across %d spellings (%s) — the "
                "scorecard tab groups by tech name, so one rep shows up as %d separate people"
                % (canon, len(variants), ", ".join(repr(v[0]) for v in variants), len(variants)))
    for canon in grouped:
        if canon not in I:
            add("WARN", "roster", "scorecards.json carries tech '%s' with no INITIAL_DATA row" % canon)

# same check at the source, if the engine's livecards are alongside us
lc_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "livecards")
if os.path.isdir(lc_dir):
    raw = Counter()
    for fn in os.listdir(lc_dir):
        if not fn.endswith(".card.json"):
            continue
        try:
            t = json.load(open(os.path.join(lc_dir, fn), encoding="utf-8")).get("tech")
        except Exception:
            continue
        if t:
            raw[t] += 1
    grouped = defaultdict(list)
    for k, v in raw.items():
        grouped[norm_name(k)].append((k, v))
    say("  livecards/ (source): %d raw spellings, %d real techs" % (len(raw), len(grouped)))
    for canon, variants in sorted(grouped.items()):
        if len(variants) > 1:
            add("FAIL", "names", "livecards/ writes '%s' under %d spellings (%s) — this is the "
                "source of the scorecards split" % (canon, len(variants),
                                                    ", ".join(repr(v[0]) for v in variants)))
    for canon in grouped:
        if canon not in I and canon.upper() == canon:
            add("WARN", "quality", "livecards/ has a non-person tech value %r" % canon)

if cj:
    say("  coaching.json: %d reps today, %d skipped, %d in history"
        % (len(cj.get("reps", [])), len(cj.get("skipped", [])), len(cj.get("history", {}))))
    for n in cj.get("history", {}):
        if n not in I:
            add("INFO", "roster", "coaching history carries '%s' with no INITIAL_DATA row" % n)

# ═══════════════════════════════════════════════════ findings
print("\n" + "=" * 78 + "\nFINDINGS\n" + "=" * 78)
rank = {"FAIL": 0, "WARN": 1, "INFO": 2}
FINDINGS.sort(key=lambda x: (rank[x[0]], x[1]))
cnt = Counter(s for s, _, _ in FINDINGS)
print("  FAIL=%d  WARN=%d  INFO=%d\n" % (cnt["FAIL"], cnt["WARN"], cnt["INFO"]))
grouped = defaultdict(list)
for s, ar, t in FINDINGS:
    grouped[(s, ar)].append(t)
for (s, ar), items in grouped.items():
    print("[%s] %s (%d)" % (s, ar, len(items)))
    for t in items[:8]:
        print("   - " + t)
    if len(items) > 8:
        print("   ... and %d more" % (len(items) - 8))
    print()
if not FINDINGS:
    print("  Everything published reconciles.\n")
sys.exit(1 if cnt["FAIL"] else 0)
