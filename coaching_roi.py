#!/usr/bin/env python3
"""Coaching ROI engine — "is the coaching program actually making money?"

THE QUESTION: every tech gets a daily coaching plan naming ONE specific gap
("build 4-6 options instead of 1-3", "offer the membership", "set expectations
so the close doesn't drag"). If coaching works the behaviour moves, and a
behaviour move should land in dollars — bigger average ticket, more
memberships, higher close rate. coaching_outcomes.py already answers "did the
behaviour move?". This engine answers the next question: "what was that worth?"
— and, critically, refuses to answer it when the data can't support an answer.

DESIGN STANCE (read this before trusting any number it emits):
  1. Every dollar figure is either computed from arithmetic that is fully
     exposed in the output (`formula` + `inputs` on every channel) or it is
     null with a `blockedBy` list saying exactly what is missing.
  2. No coefficient is hardcoded. The options-per-opp -> average-ticket
     relationship is ESTIMATED FROM THIS DATASET (OLS across the per-tech
     panel, unweighted and jobs-weighted, replicated across independent
     windows). If it fails the acceptance test, the dollar figure is null and
     the reason is stated. It is not replaced with an assumed multiplier.
  3. A membership is only worth a dollar figure if the dataset contains a
     membership price. It does not (nothing in servicedata.json /
     servicefeed.json carries SAM pricing), so membership dollars are null and
     the price is listed in requiredInputs[] as something a human must supply.
  4. With ~1 week of per-tech behaviour history the correct department answer
     is "insufficient data". That is what this emits. A fabricated ROI number
     would be worse than none because it would be used in real decisions.

INPUTS (read-only; no ServiceTitan calls — consumes what the other engines
already publish, so this is cheap enough to run every Pulse cycle):
  - coaching_outcomes.json   <- the tech-day fact table (`days`) + per-plan
    before/after rows (`outcomes`). This engine re-derives its own deltas from
    `days` rather than trusting `outcomes.delta`, because ROI needs a STRICTER
    bar (opportunity-weighted, minimum days AND minimum calls per window) than
    the behaviour-only verdicts do, and needs the call counts for the dollar
    arithmetic.
  - servicedata.json         <- per-tech panels for today/wtd/mtd/ytd. The YTD
    panel (25 techs, 90-560 opportunity calls each) is the only sample in the
    building large enough to even attempt an options->ticket relationship.
  - servicefeed.json         <- today's per-job business unit, used to capture
    each tech's maintenance-vs-demand call mix into this engine's own panel.
    Call mix is the single biggest confounder on average ticket and nothing
    has ever persisted it per tech; capturing it now is what makes a
    call-mix-controlled estimate possible later.
  - service_budget.json      <- working days + revenue target for monthising
    a per-day effect.

OUTPUT: coaching_roi.json (written locally AND published to the dashboard repo
with the same GitHub contents-API helper the other engines use):
  panel          {date: {tech: {...}}}  self-captured tech-day observations
                                        (optionsPerOpp, avgTicket, jobs, opps,
                                        closeRate, offer rate, call mix). This
                                        is the dataset a trustworthy answer
                                        will eventually be computed from —
                                        the engine bootstraps it starting the
                                        first day it runs.
  relationships  the empirically estimated (or explicitly NOT established)
                 links between behaviour and dollars, with full regression
                 output so the estimate can be audited or rejected
  techs[]        per coached tech: behaviour delta (or why not measurable) +
                 dollar translation per channel with exposed arithmetic
  department     roll-up with an explicit confidence label
  requiredInputs[] things a human must supply that the data cannot provide
  readiness      what is missing and the earliest date each answer unlocks
  assumptions[] / limitations[]  every modelling choice in plain English

Schedule: run right after coaching_outcomes.py in the same Pulse cycle.
"""
import base64, json, math, os, sys, datetime as dt
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

KEEP_DAYS = 120          # panel retention — long enough for a fixed-effects fit

# ── acceptance bars (deliberately strict; a monetized number has to earn it) ─
MIN_DAYS_PER_WINDOW = 3   # measured tech-days needed on EACH side of a plan
MIN_OPPS_PER_WINDOW = 10  # opportunity calls needed on EACH side of a plan
WINDOW_LEN = 5            # business days looked at either side of the plan

REL_MIN_POINTS = 12       # techs needed in a cross-section to fit at all
REL_MAX_P = 0.05          # two-sided p a relationship must clear
REL_MIN_OPPS = 20         # a tech needs this many opps to enter the fit

WITHIN_MIN_DAYS = 20      # distinct days needed before a within-tech fit is tried
WITHIN_MIN_TECHDAYS = 300
WITHIN_MIN_DAYS_PER_TECH = 10

DEPT_SUPPORTED_MIN_TECHS = 8
DEPT_SUPPORTED_MIN_PANEL_DAYS = 40
DEPT_DIRECTIONAL_MIN_TECHS = 3


# ── GitHub contents-API helpers (same pattern as service_data.py /
# coaching_outcomes.py — reused verbatim, not reinvented) ───────────────────
def _gh_headers(extra=None):
    h = {"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
         "Accept": "application/vnd.github+json", "User-Agent": "coaching-roi"}
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
    """No-auth fetch of the current PUBLIC file (works without DASHBOARD_TOKEN;
    used for every input, since this engine never needs write access to read)."""
    req = urllib.request.Request(RAW + path, headers={"User-Agent": "coaching-roi"})
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


def r2(x, nd=2):
    return None if x is None else round(x, nd)


# ── statistics (pure stdlib; no numpy/scipy in this repo's requirements) ────
def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def _betai(a, b, x):
    """Regularized incomplete beta I_x(a,b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t, df):
    """P(|T| > |t|) for Student-t with df degrees of freedom."""
    if df <= 0 or t is None:
        return None
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def ols(points, weights=None):
    """Simple OLS y = a + b*x. points = [(x, y)]. Returns full diagnostics or
    None if the fit is degenerate. weights (same length) gives WLS."""
    n = len(points)
    if n < 3:
        return None
    w = list(weights) if weights else [1.0] * n
    W = sum(w)
    if W <= 0:
        return None
    mx = sum(wi * p[0] for wi, p in zip(w, points)) / W
    my = sum(wi * p[1] for wi, p in zip(w, points)) / W
    sxx = sum(wi * (p[0] - mx) ** 2 for wi, p in zip(w, points))
    syy = sum(wi * (p[1] - my) ** 2 for wi, p in zip(w, points))
    sxy = sum(wi * (p[0] - mx) * (p[1] - my) for wi, p in zip(w, points))
    if sxx <= 0 or syy <= 0:
        return None
    slope = sxy / sxx
    intercept = my - slope * mx
    r = sxy / math.sqrt(sxx * syy)
    df = n - 2
    resid_ss = max(syy - slope * sxy, 0.0)
    se = math.sqrt(resid_ss / df / sxx) if df > 0 and sxx > 0 else None
    t = slope / se if se else None
    p = t_two_sided_p(t, df) if t is not None else None
    ci = None
    if se is not None:
        ci = [r2(slope - 1.96 * se, 1), r2(slope + 1.96 * se, 1)]
    return {"n": n, "slope": r2(slope, 2), "intercept": r2(intercept, 2),
            "r": r2(r, 3), "r2": r2(r * r, 3), "tStat": r2(t, 2),
            "pValue": r2(p, 4) if p is not None else None,
            "slope95ci": ci, "meanX": r2(mx, 2), "meanY": r2(my, 1),
            "weighted": bool(weights)}


def buckets(points, k=3):
    """Bucket y by terciles of x — the eyeball check that sits beside the
    regression, so a reader who distrusts OLS can still see the shape."""
    pts = sorted(points)
    n = len(pts)
    if n < k * 2:
        return None
    out = []
    for i in range(k):
        lo = i * n // k
        hi = (i + 1) * n // k
        chunk = pts[lo:hi]
        if not chunk:
            continue
        out.append({"bucket": i + 1, "n": len(chunk),
                    "xRange": [r2(chunk[0][0], 2), r2(chunk[-1][0], 2)],
                    "meanX": r2(sum(c[0] for c in chunk) / len(chunk), 2),
                    "meanY": r2(sum(c[1] for c in chunk) / len(chunk), 1)})
    return out


# ── business-day helpers ───────────────────────────────────────────────────
def biz_days_after(d0, n):
    out, cur = [], d0
    while len(out) < n:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            out.append(cur)
    return out


def windows_around(date_iso, day_keys):
    """(before_dates, after_dates): up to WINDOW_LEN Mon-Fri dates that exist
    in the fact table strictly before / after the plan date, capped at TODAY."""
    d0 = dt.date.fromisoformat(date_iso)
    keys = set(day_keys)
    before, cur = [], d0 - dt.timedelta(days=1)
    while len(before) < WINDOW_LEN and cur >= d0 - dt.timedelta(days=28):
        if cur.weekday() < 5 and cur.isoformat() in keys:
            before.append(cur.isoformat())
        cur -= dt.timedelta(days=1)
    after, cur = [], d0 + dt.timedelta(days=1)
    while len(after) < WINDOW_LEN and cur <= TODAY:
        if cur.weekday() < 5 and cur.isoformat() in keys:
            after.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return sorted(before), sorted(after)


# ── 1. self-captured tech-day panel ────────────────────────────────────────
def observation_date(sd_live, feed):
    """The date the live artifacts actually DESCRIBE — not necessarily today.

    servicedata.json / servicefeed.json carry the 'today' window as of the last
    upstream run. If this engine runs after midnight but before the next
    service_data.py run, the live rows still describe yesterday. Keying the
    panel on TODAY in that case would stamp yesterday's numbers onto today and
    silently corrupt the panel, so the observation date is taken from the data
    itself (servicefeed's date, else servicedata's updated timestamp)."""
    d = (feed or {}).get("date")
    if d:
        try:
            return dt.date.fromisoformat(d)
        except ValueError:
            pass
    u = (sd_live or {}).get("updated")
    if u:
        try:
            return dt.datetime.fromisoformat(u).date()
        except ValueError:
            pass
    return TODAY


def build_panel(sd_live, feed, prev_panel, obs_date):
    """Append the latest per-tech observation to this engine's own panel.

    WHY THIS EXISTS: servicedata_history.json only ever retained
    {sales, opps, closeRate, membershipsSold} per tech-day. optionsPerOpp,
    avgTicket, jobs and call mix — the exact fields needed to estimate what an
    extra option is worth — have never been persisted per tech-day anywhere.
    coaching_outcomes.json's `days` table started capturing optionsPerOpp on
    the day it first ran; it still does not carry jobs or call mix. So this
    engine captures its own panel from the live artifacts each run. It is the
    dataset the trustworthy answer will eventually come from."""
    panel = {k: dict(v) for k, v in (prev_panel or {}).items()}
    obs_iso = obs_date.isoformat()

    # today's call mix per tech (only source that has ever existed)
    mix = {}
    for j in (feed or {}).get("jobs", []):
        name = j.get("tech")
        if not name:
            continue
        m = mix.setdefault(name, {"feedJobs": 0, "maintJobs": 0})
        m["feedJobs"] += 1
        if "maintenance" in (j.get("bu") or "").lower():
            m["maintJobs"] += 1

    day_out = {}
    for row in ((sd_live or {}).get("techs", {}) or {}).get("today", []) or []:
        name = row.get("name")
        if not name:
            continue
        m = mix.get(name, {})
        feed_jobs = m.get("feedJobs")
        maint = m.get("maintJobs")
        day_out[name] = {
            "opps": row.get("opps"),
            "jobs": row.get("jobs"),
            "revenue": row.get("revenue"),
            "avgTicket": row.get("avgTicket"),
            "avgSale": row.get("avgSale"),
            "closeRate": row.get("closeRate"),
            "optionsPerOpp": row.get("optionsPerOpp"),
            "membershipOffered": row.get("membershipOffered"),
            "membershipOfferJobs": row.get("membershipOfferJobs"),
            "membershipOfferRate": row.get("membershipOfferRate"),
            "membershipsSold": row.get("membershipsSold"),
            "feedJobs": feed_jobs,
            "maintJobs": maint,
            "maintShare": (round(maint / feed_jobs * 100, 1)
                           if feed_jobs else None),
        }
    if day_out:
        panel[obs_iso] = day_out            # live day: always overwrite
    kept = sorted(panel.keys())[-KEEP_DAYS:]
    return {d: panel[d] for d in kept}


def panel_points(panel, min_opps=1):
    """Flatten the panel into (date, tech, x=optionsPerOpp, y=avgTicket) rows
    usable for a within-tech estimate."""
    rows = []
    for date, techs in (panel or {}).items():
        for name, r in techs.items():
            x, y, opps = r.get("optionsPerOpp"), r.get("avgTicket"), r.get("opps") or 0
            if x is None or y is None or opps < min_opps:
                continue
            rows.append({"date": date, "tech": name, "x": x, "y": y,
                         "opps": opps, "jobs": r.get("jobs") or 0,
                         "maintShare": r.get("maintShare")})
    return rows


# ── 2. the empirical options -> average-ticket relationship ────────────────
def estimate_options_to_ticket(sd_live, panel):
    """Estimate, FROM THIS DATASET ONLY, how many dollars of average ticket one
    extra option-per-opportunity is associated with.

    Three independent attempts, in descending order of how much they'd be
    worth believing:
      A. within-tech (each tech compared to their own average across days) —
         the only version that removes "good techs both build more options AND
         sell more". Needs a panel this engine has to accumulate first.
      B. cross-tech YTD (n=25 techs, hundreds of calls each) — biggest sample
         available today, but every point is a different person, so a positive
         slope could just mean better techs build more options and a negative
         slope could just mean maintenance-heavy techs build more options on
         cheaper calls. Confounded by call mix, which nothing controls for.
      C. cross-tech MTD / WTD / today — small and noisy; used only as a
         sign-replication check on B.

    The relationship is only declared ESTABLISHED if the primary fit clears
    p < REL_MAX_P in BOTH unweighted and jobs-weighted form and every other
    window with enough points agrees on the sign. Anything less returns
    established=false and a null dollar coefficient."""
    out = {
        "question": "What is +1 option per opportunity worth in average ticket ($/job)?",
        "method": ("OLS of per-tech average ticket on per-tech options-per-opportunity. "
                   "Fitted unweighted and weighted by job count; replicated across every "
                   "independent window available. No coefficient is assumed or imported "
                   "from outside this dataset."),
        "crossSection": {}, "withinTech": None,
        "established": False, "dollarsPerExtraOptionPerJob": None,
        "reason": None, "signAgreement": None,
    }

    windows = ["ytd", "mtd", "wtd", "today"]
    for w in windows:
        rows = ((sd_live or {}).get("techs", {}) or {}).get(w) or []
        # YTD is the only window where a meaningful per-tech sample bar is
        # possible; the short windows only ever have a handful of calls per
        # tech, so they use the same lowVolume bar (>=2 calls) the rest of the
        # system uses and are treated as sign-replication checks, not evidence.
        min_opps = REL_MIN_OPPS if w == "ytd" else 2
        pts, wts = [], []
        for r in rows:
            x, y, opps, jobs = (r.get("optionsPerOpp"), r.get("avgTicket"),
                                r.get("opps") or 0, r.get("jobs") or 0)
            if x is None or y is None or not x or opps < min_opps:
                continue
            pts.append((x, y))
            wts.append(max(jobs, 1))
        fit = ols(pts) if len(pts) >= 3 else None
        wfit = ols(pts, wts) if len(pts) >= 3 else None
        out["crossSection"][w] = {
            "points": len(pts), "minOppsFilter": min_opps,
            "unweighted": fit, "jobsWeighted": wfit,
            "buckets": buckets(pts) if len(pts) >= 6 else None,
            "usable": bool(fit) and len(pts) >= REL_MIN_POINTS,
        }

    # A. within-tech attempt
    prows = panel_points(panel, min_opps=2)
    days_n = len({r["date"] for r in prows})
    per_tech = {}
    for r in prows:
        per_tech.setdefault(r["tech"], []).append(r)
    eligible = {t: rs for t, rs in per_tech.items() if len(rs) >= WITHIN_MIN_DAYS_PER_TECH}
    within = {"techDays": len(prows), "distinctDays": days_n,
              "techsWithEnoughDays": len(eligible),
              "required": {"distinctDays": WITHIN_MIN_DAYS,
                           "techDays": WITHIN_MIN_TECHDAYS,
                           "daysPerTech": WITHIN_MIN_DAYS_PER_TECH},
              "fit": None, "status": None}
    if (days_n >= WITHIN_MIN_DAYS and len(prows) >= WITHIN_MIN_TECHDAYS
            and len(eligible) >= 5):
        # tech-demeaned OLS == fixed-effects slope for a single regressor
        demeaned = []
        wts = []
        for t, rs in eligible.items():
            mx = sum(r["x"] for r in rs) / len(rs)
            my = sum(r["y"] for r in rs) / len(rs)
            for r in rs:
                demeaned.append((r["x"] - mx, r["y"] - my))
                wts.append(max(r["jobs"], 1))
        fit = ols(demeaned)
        wfit = ols(demeaned, wts)
        within["fit"] = {"unweighted": fit, "jobsWeighted": wfit}
        within["status"] = "fitted"
    else:
        within["status"] = "not-enough-panel"
    out["withinTech"] = within

    # decide
    if within["status"] == "fitted" and within["fit"]["unweighted"]:
        f, wf = within["fit"]["unweighted"], within["fit"]["jobsWeighted"]
        ok = (f["pValue"] is not None and f["pValue"] < REL_MAX_P
              and wf and wf["pValue"] is not None and wf["pValue"] < REL_MAX_P
              and f["slope"] * wf["slope"] > 0)
        if ok:
            out["established"] = True
            out["dollarsPerExtraOptionPerJob"] = min(abs(f["slope"]), abs(wf["slope"])) * (
                1 if f["slope"] > 0 else -1)
            out["primaryEstimate"] = "withinTech"
            out["reason"] = ("Within-tech (each tech vs. their own average) slope is "
                             "significant and same-signed unweighted and jobs-weighted; "
                             "the more conservative of the two is used.")
            return out
        out["reason"] = ("Within-tech fit ran but did not clear p<%.2f in both weighted "
                         "and unweighted form." % REL_MAX_P)
        return out

    ytd = out["crossSection"].get("ytd") or {}
    f, wf = ytd.get("unweighted"), ytd.get("jobsWeighted")
    if not ytd.get("usable") or not f:
        out["reason"] = ("No usable cross-section: fewer than %d techs with >=%d "
                         "opportunity calls carry both options-per-opp and average "
                         "ticket." % (REL_MIN_POINTS, REL_MIN_OPPS))
        return out

    signs = []
    for w, blk in out["crossSection"].items():
        ff = blk.get("unweighted")
        if ff and blk["points"] >= 6:
            signs.append((w, 1 if ff["slope"] > 0 else -1, ff["slope"], ff["pValue"]))
    agree = len({s[1] for s in signs}) == 1 if signs else False
    out["signAgreement"] = {"agree": agree,
                            "byWindow": [{"window": w, "slope": sl, "pValue": p}
                                         for w, _, sl, p in signs]}

    sig = (f["pValue"] is not None and f["pValue"] < REL_MAX_P
           and wf and wf["pValue"] is not None and wf["pValue"] < REL_MAX_P
           and f["slope"] * wf["slope"] > 0)
    if not sig:
        out["reason"] = (
            "NOT ESTABLISHED. The largest sample available (YTD, %d techs) gives a slope of "
            "%s $/job per extra option with p=%s and r2=%s — it does not clear p<%.2f, so the "
            "association is statistically indistinguishable from zero. %s Monetizing an "
            "options lift would therefore mean inventing a multiplier, so the dollar figure "
            "is null." % (
                f["n"], f["slope"], f["pValue"], f["r2"], REL_MAX_P,
                ("The sign also flips across windows (%s), which is what noise looks like."
                 % ", ".join("%s %+.0f" % (w, sl) for w, _, sl, _ in signs))
                if signs and not agree else
                "The sign is consistent across windows but the magnitude is not resolvable."))
        return out
    if not agree:
        out["reason"] = ("YTD cross-section is significant but the slope sign is not "
                         "consistent across windows, so it is not treated as established.")
        return out

    out["established"] = True
    out["primaryEstimate"] = "crossSectionYtd"
    out["dollarsPerExtraOptionPerJob"] = min(abs(f["slope"]), abs(wf["slope"])) * (
        1 if f["slope"] > 0 else -1)
    out["reason"] = ("Cross-tech YTD slope significant and sign-consistent across windows; "
                     "the more conservative of unweighted/jobs-weighted is used. This is a "
                     "BETWEEN-TECH association, not a within-tech causal effect — see "
                     "limitations.")
    return out


def membership_economics(sd_live):
    """What the data does and does not tell us about turning a membership
    OFFER-rate lift into dollars."""
    dept_today = (sd_live or {}).get("today") or {}
    dept_wtd = (sd_live or {}).get("wtd") or {}
    rows = ((sd_live or {}).get("techs", {}) or {}).get("today") or []
    over_100 = [r["name"] for r in rows
                if (r.get("membershipCloseOnOffer") or 0) > 100]
    out = {
        "closeOnOfferPctToday": dept_today.get("membershipCloseOnOffer"),
        "closeOnOfferPctWtd": dept_wtd.get("membershipCloseOnOffer"),
        "reliable": False,
        "membershipPriceUsd": None,
        "priceSource": None,
        "reason": None,
    }
    problems = []
    if over_100:
        problems.append(
            "membershipCloseOnOffer exceeds 100%% for %d tech(s) today (e.g. %s), which proves the "
            "numerator (memberships sold, counted from the ServiceTitan report) and the "
            "denominator (offers detected as SAM line items on estimates) are drawn from "
            "different populations — many sold memberships never appear as a detected offer. "
            "It therefore cannot be used as a conversion rate."
            % (len(over_100), ", ".join(sorted(over_100)[:4])))
    problems.append(
        "No membership price exists anywhere in the published data — servicedata.json carries "
        "membership COUNTS only, and servicefeed.json carries no line-item pricing. A "
        "membership's annual revenue must be supplied by a human before any membership lift "
        "can be valued.")
    out["reason"] = " ".join(problems)
    return out


# ── 3. per-tech behaviour delta, re-derived at the ROI bar ─────────────────
METRIC_WEIGHT_FIELD = {
    "optionsPerOpp": "callsRun",
    "closeRate": "callsRun",
    "membershipOfferRate": "membershipOfferJobs",
    "recordedPct": "callsRun",
}
MONETIZABLE = {"optionsPerOpp", "closeRate", "membershipOfferRate"}


def weighted_window(days, tech, dates, metric):
    """Opportunity-weighted mean of `metric` over `dates` for `tech`.
    Weighting matters: an unweighted mean lets a 2-call day count as much as an
    8-call day, and the dollar math needs the call counts anyway."""
    num = den = 0.0
    used, opps = [], 0
    wfield = METRIC_WEIGHT_FIELD.get(metric, "callsRun")
    for d in dates:
        row = (days.get(d) or {}).get(tech)
        if not row:
            continue
        v = row.get(metric)
        if v is None:
            continue
        if row.get("lowVolume"):
            continue
        w = row.get(wfield) or row.get("callsRun") or 0
        if not w:
            continue
        num += v * w
        den += w
        used.append(d)
        opps += row.get("callsRun") or 0
    if not den:
        return None
    return {"value": r2(num / den, 2), "days": len(used), "dates": used,
            "weightTotal": int(den), "opps": opps}


def tech_behaviour_delta(days, tech, plan_date, metric):
    before_dates, after_dates = windows_around(plan_date, days.keys())
    pre = weighted_window(days, tech, before_dates, metric)
    post = weighted_window(days, tech, after_dates, metric)
    blocked = []
    if pre is None or pre["days"] < MIN_DAYS_PER_WINDOW:
        blocked.append("pre-window has %d measured day(s), needs %d"
                       % (0 if pre is None else pre["days"], MIN_DAYS_PER_WINDOW))
    if post is None or post["days"] < MIN_DAYS_PER_WINDOW:
        blocked.append("post-window has %d measured day(s), needs %d"
                       % (0 if post is None else post["days"], MIN_DAYS_PER_WINDOW))
    if pre and pre["opps"] < MIN_OPPS_PER_WINDOW:
        blocked.append("pre-window has %d opportunity call(s), needs %d"
                       % (pre["opps"], MIN_OPPS_PER_WINDOW))
    if post and post["opps"] < MIN_OPPS_PER_WINDOW:
        blocked.append("post-window has %d opportunity call(s), needs %d"
                       % (post["opps"], MIN_OPPS_PER_WINDOW))
    delta = None
    if not blocked and pre and post:
        delta = r2(post["value"] - pre["value"], 2)
    return {"metric": metric, "pre": pre, "post": post, "delta": delta,
            "attributable": delta is not None, "blockedBy": blocked,
            "windowBusinessDays": WINDOW_LEN,
            "bar": {"minMeasuredDaysPerWindow": MIN_DAYS_PER_WINDOW,
                    "minOppsPerWindow": MIN_OPPS_PER_WINDOW}}


# ── 4. dollar translation ──────────────────────────────────────────────────
def tech_ref(sd_live, window, name):
    for r in ((sd_live or {}).get("techs", {}) or {}).get(window, []) or []:
        if r.get("name") == name:
            return r
    return None


def monetize(tech, beh, rel, mem_econ, sd_live, budget):
    """Translate a behaviour delta into monthly dollars. EVERY channel returns
    a `formula` string and an `inputs` dict so the arithmetic can be audited
    line by line, and returns value=None with blockedBy[] when any input is
    missing rather than substituting a guess."""
    metric = beh["metric"]
    working_days = (budget or {}).get("workingDays")
    ytd = tech_ref(sd_live, "ytd", tech) or {}
    mtd = tech_ref(sd_live, "mtd", tech) or {}
    post = beh.get("post") or {}
    delta = beh.get("delta")

    # observed run-rate for this tech, taken from the post window if we have
    # one, else from MTD, else YTD — always labelled with which was used.
    # Run-rate comes from the post window only. MTD/YTD carry totals but no
    # per-tech day count (a tech's days off aren't recorded), so a calls-per-day
    # figure cannot be derived from them without guessing — left null instead.
    opps_per_day = None
    rate_src = None
    if post.get("days"):
        opps_per_day = post["opps"] / post["days"]
        rate_src = "post-window observed"
    jobs_per_opp = None
    if ytd.get("opps"):
        jobs_per_opp = (ytd.get("jobs") or 0) / ytd["opps"]

    blocked = []
    ch = {"channel": None, "value": None, "formula": None, "inputs": {},
          "blockedBy": blocked}

    if metric == "optionsPerOpp":
        beta = rel.get("dollarsPerExtraOptionPerJob")
        ch["channel"] = "avgTicket-from-options"
        ch["formula"] = ("monthlyUsd = deltaOptionsPerOpp * dollarsPerExtraOptionPerJob "
                         "* jobsPerOpp * oppsPerWorkingDay * workingDaysInMonth")
        ch["inputs"] = {
            "deltaOptionsPerOpp": delta,
            "dollarsPerExtraOptionPerJob": beta,
            "jobsPerOpp": r2(jobs_per_opp, 3),
            "jobsPerOppSource": "tech YTD jobs / YTD opportunity calls" if jobs_per_opp else None,
            "oppsPerWorkingDay": r2(opps_per_day, 2),
            "oppsPerWorkingDaySource": rate_src,
            "workingDaysInMonth": working_days,
        }
        if delta is None:
            blocked.append("no attributable behaviour delta yet")
        if beta is None:
            blocked.append("options->average-ticket relationship not established in this "
                           "dataset (see relationships.optionsToAvgTicket.reason)")
        if jobs_per_opp is None:
            blocked.append("no YTD jobs/opps ratio for this tech")
        if opps_per_day is None:
            blocked.append("no observed opportunity-calls-per-day for this tech")
        if not blocked:
            ch["value"] = round(delta * beta * jobs_per_opp * opps_per_day * working_days)
        return ch

    if metric == "membershipOfferRate":
        price = mem_econ.get("membershipPriceUsd")
        close_on_offer = mem_econ.get("closeOnOfferPctToday") if mem_econ.get("reliable") else None
        offer_jobs_per_day = None
        if post.get("days") and post.get("weightTotal"):
            offer_jobs_per_day = post["weightTotal"] / post["days"]
        ch["channel"] = "memberships-from-offer-rate"
        ch["formula"] = ("monthlyUsd = (deltaOfferRatePct / 100) * offerEligibleJobsPerWorkingDay "
                         "* workingDaysInMonth * (closeOnOfferPct / 100) * membershipPriceUsd")
        ch["inputs"] = {
            "deltaOfferRatePct": delta,
            "offerEligibleJobsPerWorkingDay": r2(offer_jobs_per_day, 2),
            "workingDaysInMonth": working_days,
            "closeOnOfferPct": close_on_offer,
            "membershipPriceUsd": price,
        }
        if delta is None:
            blocked.append("no attributable behaviour delta yet")
        if close_on_offer is None:
            blocked.append("observed close-on-offer is not a usable conversion rate "
                           "(see relationships.membershipEconomics.reason)")
        if price is None:
            blocked.append("membership price is not present anywhere in the data — "
                           "must be supplied (see requiredInputs)")
        if offer_jobs_per_day is None:
            blocked.append("no observed offer-eligible jobs per day for this tech")
        if not blocked:
            ch["value"] = round(delta / 100 * offer_jobs_per_day * working_days
                                * close_on_offer / 100 * price)
        return ch

    if metric == "closeRate":
        avg_sale = ytd.get("avgSale") or mtd.get("avgSale")
        avg_sale_src = ("tech YTD average sale (revenue per sold job)" if ytd.get("avgSale")
                        else ("tech MTD average sale" if mtd.get("avgSale") else None))
        ch["channel"] = "incremental-sold-jobs-from-close-rate"
        ch["formula"] = ("monthlyUsd = (deltaCloseRatePct / 100) * oppsPerWorkingDay "
                         "* workingDaysInMonth * avgSaleUsd")
        ch["inputs"] = {
            "deltaCloseRatePct": delta,
            "oppsPerWorkingDay": r2(opps_per_day, 2),
            "oppsPerWorkingDaySource": rate_src,
            "workingDaysInMonth": working_days,
            "avgSaleUsd": avg_sale,
            "avgSaleSource": avg_sale_src,
            "avgSaleIsApprox": ytd.get("salesIsApprox"),
        }
        if delta is None:
            blocked.append("no attributable behaviour delta yet")
        if avg_sale is None:
            blocked.append("no observed average sale for this tech")
        if opps_per_day is None:
            blocked.append("no observed opportunity-calls-per-day for this tech")
        if not blocked:
            ch["value"] = round(delta / 100 * opps_per_day * working_days * avg_sale)
        return ch

    ch["channel"] = "none"
    blocked.append("coached focus does not map to a monetizable metric "
                   "(recording compliance and rapport/process gaps have no direct "
                   "dollar channel in this model)")
    return ch


# ── 5. assembly ────────────────────────────────────────────────────────────
def build_techs(outcomes, days, rel, mem_econ, sd_live, budget):
    rows = []
    seen = set()
    for oc in outcomes or []:
        tech, date, metric = oc.get("tech"), oc.get("date"), oc.get("metric")
        if not tech or not date:
            continue
        key = (tech, date, metric)
        if key in seen:
            continue
        seen.add(key)
        entry = {"tech": tech, "planDate": date, "focusText": oc.get("focusText"),
                 "metric": metric, "behaviourVerdict": oc.get("verdict")}
        if metric is None:
            entry["behaviour"] = {
                "metric": None, "delta": None, "attributable": False,
                "blockedBy": ["coaching focus text does not name a metric this system "
                              "measures (or the focus text was never captured for that "
                              "plan date)"]}
            entry["dollars"] = {"channel": "none", "value": None, "formula": None,
                                "inputs": {},
                                "blockedBy": ["no measurable metric for this focus"]}
            entry["monthlyUsd"] = None
            rows.append(entry)
            continue
        beh = tech_behaviour_delta(days, tech, date, metric)
        entry["behaviour"] = beh
        if metric not in MONETIZABLE:
            entry["dollars"] = {
                "channel": "none", "value": None, "formula": None, "inputs": {},
                "blockedBy": ["metric '%s' has no direct revenue channel in this model"
                              % metric]}
            entry["monthlyUsd"] = None
        else:
            ch = monetize(tech, beh, rel, mem_econ, sd_live, budget)
            entry["dollars"] = ch
            entry["monthlyUsd"] = ch.get("value")
        rows.append(entry)
    rows.sort(key=lambda r: (r["tech"], r["planDate"]))
    return rows


def build_department(techs, panel, rel, budget):
    monetized = [t for t in techs if t.get("monthlyUsd") is not None]
    attributable = [t for t in techs if (t.get("behaviour") or {}).get("attributable")]
    panel_days = len(panel or {})
    by_channel = {}
    for t in monetized:
        ch = (t.get("dollars") or {}).get("channel") or "none"
        by_channel[ch] = by_channel.get(ch, 0) + t["monthlyUsd"]

    if len(monetized) < DEPT_DIRECTIONAL_MIN_TECHS:
        conf = "insufficient-data"
        total = None
    elif (len(monetized) >= DEPT_SUPPORTED_MIN_TECHS
          and panel_days >= DEPT_SUPPORTED_MIN_PANEL_DAYS
          and rel.get("primaryEstimate") == "withinTech"):
        conf = "supported"
        total = round(sum(t["monthlyUsd"] for t in monetized))
    else:
        conf = "directional"
        total = round(sum(t["monthlyUsd"] for t in monetized))

    reasons = []
    if len(monetized) < DEPT_DIRECTIONAL_MIN_TECHS:
        reasons.append("only %d of %d coached techs have a monetizable, attributable "
                       "behaviour delta (need >=%d for even a directional number)"
                       % (len(monetized), len(techs), DEPT_DIRECTIONAL_MIN_TECHS))
    if len(attributable) == 0:
        reasons.append("zero coaching plans currently have enough measured days on BOTH "
                       "sides of the plan date to compute an attributable behaviour delta")
    if not rel.get("established"):
        reasons.append("the options-per-opp -> average-ticket link is not established, so "
                       "the largest dollar channel cannot be valued at all")
    if panel_days < DEPT_SUPPORTED_MIN_PANEL_DAYS:
        reasons.append("this engine's own tech-day panel is %d day(s) deep; a "
                       "call-mix-controlled within-tech estimate needs >=%d"
                       % (panel_days, DEPT_SUPPORTED_MIN_PANEL_DAYS))

    return {
        "confidence": conf,
        "estimatedMonthlyImpactUsd": total,
        "estimatedMonthlyImpactByChannel": by_channel or None,
        "asPctOfMonthlyRevenueTarget": (
            r2(total / budget["revenueTarget"] * 100, 2)
            if total is not None and (budget or {}).get("revenueTarget") else None),
        "coachedTechsEvaluated": len(techs),
        "techsWithAttributableBehaviourDelta": len(attributable),
        "techsWithMonetizedImpact": len(monetized),
        "panelDaysCaptured": panel_days,
        "whyNotHigherConfidence": reasons,
        "confidenceLabelRules": {
            "insufficient-data": "fewer than %d techs with a monetizable attributable delta"
                                 % DEPT_DIRECTIONAL_MIN_TECHS,
            "directional": "at least %d monetizable techs, but either the panel is shallower "
                           "than %d days or the dollar coefficient still rests on a "
                           "between-tech (not within-tech) association"
                           % (DEPT_DIRECTIONAL_MIN_TECHS, DEPT_SUPPORTED_MIN_PANEL_DAYS),
            "supported": "at least %d monetizable techs, >=%d days of panel, and the dollar "
                         "coefficient comes from a within-tech fit. Still NOT proof of "
                         "causation — see limitations."
                         % (DEPT_SUPPORTED_MIN_TECHS, DEPT_SUPPORTED_MIN_PANEL_DAYS),
        },
    }


def build_readiness(days, panel, rel):
    """Say precisely what is missing and when each answer unlocks, in dates."""
    first_by_metric = {}
    for metric in ("optionsPerOpp", "membershipOfferRate", "closeRate"):
        firsts = [d for d in sorted(days or {})
                  if any((r or {}).get(metric) is not None for r in days[d].values())]
        first_by_metric[metric] = firsts[0] if firsts else None

    unlocks = {}
    for metric, first in first_by_metric.items():
        if not first:
            unlocks[metric] = {"firstDayWithData": None,
                               "earliestEvaluablePlanDate": None,
                               "earliestVerdictDate": None,
                               "note": "no day has ever carried this metric"}
            continue
        d0 = dt.date.fromisoformat(first)
        # need MIN_DAYS_PER_WINDOW business days of data BEFORE a plan, then
        # MIN_DAYS_PER_WINDOW business days AFTER it
        pre_done = biz_days_after(d0, MIN_DAYS_PER_WINDOW - 1)[-1] if MIN_DAYS_PER_WINDOW > 1 else d0
        plan_day = biz_days_after(pre_done, 1)[0]
        verdict_day = biz_days_after(plan_day, MIN_DAYS_PER_WINDOW)[-1]
        unlocks[metric] = {
            "firstDayWithData": first,
            "earliestEvaluablePlanDate": plan_day.isoformat(),
            "earliestVerdictDate": verdict_day.isoformat(),
            "alreadyReached": verdict_day <= TODAY,
        }

    panel_days = len(panel or {})
    needed_days = max(0, WITHIN_MIN_DAYS - panel_days)
    proj = biz_days_after(TODAY, needed_days)[-1].isoformat() if needed_days else TODAY.isoformat()
    needed_days_supported = max(0, DEPT_SUPPORTED_MIN_PANEL_DAYS - panel_days)
    proj_supported = (biz_days_after(TODAY, needed_days_supported)[-1].isoformat()
                      if needed_days_supported else TODAY.isoformat())

    return {
        "behaviourDeltaUnlock": unlocks,
        "dollarCoefficientUnlock": {
            "status": rel.get("reason"),
            "panelDaysCaptured": panel_days,
            "panelTechDaysCaptured": (rel.get("withinTech") or {}).get("techDays"),
            "withinTechFitNeeds": {"distinctDays": WITHIN_MIN_DAYS,
                                   "techDays": WITHIN_MIN_TECHDAYS,
                                   "daysPerTech": WITHIN_MIN_DAYS_PER_TECH},
            "earliestWithinTechFitDate": proj,
            "earliestSupportedLabelDate": proj_supported,
            "note": ("Dates assume the panel keeps accruing one business day per run with "
                     "roughly today's tech count. They are the EARLIEST possible dates, not "
                     "promises — a fit can still fail to reach significance on that date."),
        },
        "whatWouldMakeThisTrustworthy": [
            "About 8 weeks (~40 business days, ~850-900 tech-day observations at today's "
            "22-tech roster) of this engine's panel, which is the point at which a "
            "within-tech, call-mix-controlled estimate of what an extra option is worth "
            "becomes fittable at all.",
            "A membership price (annual or monthly revenue per membership sold). One number "
            "from finance unblocks the entire membership channel immediately.",
            "A membership offer counter whose numerator and denominator come from the same "
            "population. Today's close-on-offer exceeds 100% for some techs, which means it "
            "is not a conversion rate and cannot be used in any dollar model.",
            "A holdout or staggered rollout: a group of techs who do NOT receive daily plans "
            "for a defined period, or plans deliberately started in waves. Without one, no "
            "amount of extra data upgrades this from association to proven causation, because "
            "seasonality, lead mix and price changes move the same metrics.",
            "Per-tech-day job counts and revenue retained permanently (this engine now "
            "captures them; servicedata_history.json never did), so the panel is not lost "
            "if a run is missed.",
        ],
    }


ASSUMPTIONS = [
    "A coaching plan's effect, if any, is assumed to show up within the %d business days "
    "after it is issued and to be absent in the %d business days before it. Anything slower "
    "than that is invisible to this model." % (WINDOW_LEN, WINDOW_LEN),
    "Before/after metric values are opportunity-weighted (a 6-call day counts three times a "
    "2-call day), not simple day averages, so one quiet day cannot swing a verdict.",
    "Tech-days flagged lowVolume (fewer than 2 opportunity calls) are excluded from both "
    "windows entirely.",
    "A delta is only called attributable if BOTH windows have at least %d measured days AND "
    "at least %d opportunity calls. This is stricter than coaching_outcomes.py's behaviour "
    "verdicts on purpose: a number that turns into dollars has to clear a higher bar than a "
    "number that only turns into a coaching note." % (MIN_DAYS_PER_WINDOW, MIN_OPPS_PER_WINDOW),
    "Monthly impact projects a per-working-day effect across the month's working-day count "
    "from service_budget.json. It assumes the observed post-window call rate continues and "
    "that the behaviour change persists for the whole month — both optimistic.",
    "The close-rate channel values an incremental sold job at the tech's own observed average "
    "sale, not at the department average, and uses revenue per sold job (which for YTD is the "
    "report-revenue fallback, flagged as approximate in the output).",
    "The options channel needs a dollars-per-extra-option coefficient. That coefficient is "
    "estimated from this dataset only — never assumed — and is left null whenever the "
    "estimate fails its significance and sign-replication test.",
    "Every dollar figure is gross revenue, not gross profit and not net of the cost of running "
    "the coaching program. Nobody should read these numbers as profit.",
]

LIMITATIONS = [
    "CORRELATION IS NOT CAUSATION HERE. There is no control group: every tech receives daily "
    "coaching plans, so there is no un-coached comparison. Any before/after movement is "
    "confounded with seasonality, weather-driven demand, lead mix, pricing changes, the "
    "specific jobs dispatched that week, and simple regression to the mean (techs are coached "
    "precisely BECAUSE they just had a bad stretch, and a bad stretch tends to be followed by "
    "a more average one whether or not anyone coaches them).",
    "Per-tech optionsPerOpp, membershipOfferRate, average ticket and revenue have only been "
    "persisted per tech-day since this system started capturing them. servicedata_history.json "
    "retained only {sales, opps, closeRate, membershipsSold}, so earlier days cannot be "
    "reconstructed. The usable history is days, not months.",
    "Coaching focus text is only fully retained for the most recent plan date; older plans keep "
    "a file link but not the focus, so their outcome cannot be classified to a metric at all.",
    "Average ticket is confounded by call mix. A tech running mostly maintenance tune-ups will "
    "show a low average ticket regardless of how many options they build, and nothing in the "
    "historical data controls for that. This engine now captures per-tech maintenance share "
    "daily so a future estimate can control for it; it could not be controlled for "
    "retroactively.",
    "Any cross-tech relationship compares different people, so it cannot separate 'building "
    "more options raises the ticket' from 'stronger techs both build more options and sell "
    "more'. Only a within-tech estimate can, and that needs a panel this engine is still "
    "accumulating.",
    "The membership offer rate's numerator (SAM items detected on estimates) and the "
    "memberships-sold count come from different pipelines; close-on-offer above 100% for some "
    "techs proves they do not describe the same population.",
    "One coaching plan names one gap, but techs receive plans on consecutive days and multiple "
    "gaps overlap. This model attributes a metric move to the plan that named that metric; it "
    "cannot separate overlapping plans, other coaching, ride-alongs, pricebook changes or "
    "manager pressure happening at the same time.",
    "Techs whose coached gap is rapport, process or recording compliance have no dollar channel "
    "in this model. Their coaching may well be valuable and will read as zero here, so the "
    "department roll-up is a floor on measured channels, not a measure of the program.",
]


def main():
    dry = "--dry" in sys.argv
    print("[coaching_roi] start %s" % NOW.isoformat())

    outcomes_doc = raw_json("coaching_outcomes.json")
    if not outcomes_doc:
        local = os.path.join(HERE, "coaching_outcomes.json")
        if os.path.exists(local):
            outcomes_doc = json.load(open(local))
            print("  coaching_outcomes.json: local fallback")
    sd_live = raw_json("servicedata.json")
    feed = raw_json("servicefeed.json")

    budget = None
    bpath = os.path.join(HERE, "service_budget.json")
    if os.path.exists(bpath):
        budget = json.load(open(bpath))
    else:
        budget = raw_json("service_budget.json")

    prev, prev_sha = (None, None)
    if not dry and "DASHBOARD_TOKEN" in os.environ:
        prev, prev_sha = gh_fetch("coaching_roi.json")
    prev_json = json.loads(prev) if prev else None
    if prev_json is None:
        lp = os.path.join(HERE, "coaching_roi.json")
        if os.path.exists(lp):
            try:
                prev_json = json.load(open(lp))
            except Exception:
                prev_json = None
    prev_panel = (prev_json or {}).get("panel", {})

    days = (outcomes_doc or {}).get("days", {})
    outcomes = (outcomes_doc or {}).get("outcomes", [])
    print("  coaching_outcomes.json: %s (%d day(s), %d outcome row(s))"
          % ("present" if outcomes_doc else "MISSING", len(days), len(outcomes)))
    print("  servicedata.json live: %s" % ("present" if sd_live else "MISSING"))
    print("  servicefeed.json live: %s" % ("present" if feed else "MISSING"))
    print("  service_budget.json: %s" % ("present" if budget else "MISSING"))
    print("  prior panel days: %d" % len(prev_panel))

    obs_date = observation_date(sd_live, feed)
    if obs_date != TODAY:
        print("  NOTE: live artifacts describe %s, not %s — panel row keyed to the "
              "date the data describes." % (obs_date.isoformat(), TODAY.isoformat()))
    panel = build_panel(sd_live, feed, prev_panel, obs_date)
    rel = estimate_options_to_ticket(sd_live, panel)
    mem_econ = membership_economics(sd_live)
    techs = build_techs(outcomes, days, rel, mem_econ, sd_live, budget)
    dept = build_department(techs, panel, rel, budget)
    readiness = build_readiness(days, panel, rel)

    required_inputs = [{
        "key": "membershipMonthlyOrAnnualRevenueUsd",
        "why": "Nothing in the published data carries a membership price, so a membership "
               "offer-rate lift cannot be converted to dollars.",
        "whoProvides": "Finance / John — one number, the revenue a sold membership is worth.",
        "blocks": ["relationships.membershipEconomics.membershipPriceUsd",
                   "every techs[].dollars entry with channel 'memberships-from-offer-rate'"],
    }, {
        "key": "membershipCloseOnOfferFromMatchedPopulation",
        "why": "Observed close-on-offer exceeds 100% for some techs, so offers and sales are "
               "not counted over the same jobs and the ratio is not a conversion rate.",
        "whoProvides": "A fix in the offer-detection pipeline (servicefeed_sync/service_data), "
                       "not a human estimate.",
        "blocks": ["every techs[].dollars entry with channel 'memberships-from-offer-rate'"],
    }, {
        "key": "coachingProgramCost",
        "why": "Everything here is gross revenue. Net ROI needs the program's cost "
               "(tooling + manager and tech time).",
        "whoProvides": "John.",
        "blocks": ["any true return-on-investment ratio; this engine only estimates return"],
    }]

    headline = None
    if dept["confidence"] == "insufficient-data":
        headline = (
            "NOT YET ANSWERABLE. %d coaching plans were evaluated; %d have enough measured "
            "days on both sides of the plan date to show an attributable behaviour change, "
            "and %d can be converted to dollars. The honest answer to 'what is the coaching "
            "program worth?' today is that the data does not support a number — not that the "
            "number is zero." % (len(techs), dept["techsWithAttributableBehaviourDelta"],
                                 dept["techsWithMonetizedImpact"]))
    else:
        headline = ("Estimated monthly impact $%s (%s confidence, %d tech(s) measured). "
                    "Association only — no control group."
                    % (format(dept["estimatedMonthlyImpactUsd"], ","),
                       dept["confidence"], dept["techsWithMonetizedImpact"]))

    out = {
        "generated": NOW.isoformat(),
        "headline": headline,
        "department": dept,
        "relationships": {"optionsToAvgTicket": rel, "membershipEconomics": mem_econ},
        "techs": techs,
        "requiredInputs": required_inputs,
        "readiness": readiness,
        "assumptions": ASSUMPTIONS,
        "limitations": LIMITATIONS,
        "panel": panel,
        "panelNote": ("Tech-day observations captured by this engine from the live "
                      "servicedata.json/servicefeed.json each run. This is the dataset a "
                      "defensible dollar coefficient will eventually be fitted on; it starts "
                      "the first day this engine runs and cannot be backfilled."),
        "inputsSeen": {
            "coachingOutcomesDays": len(days),
            "coachingOutcomesRows": len(outcomes),
            "servicedataLive": bool(sd_live),
            "servicefeedLive": bool(feed),
            "liveObservationDate": obs_date.isoformat(),
            "budgetMonth": (budget or {}).get("month"),
        },
    }

    local_path = os.path.join(HERE, "coaching_roi.json")
    with open(local_path + ".tmp", "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    os.replace(local_path + ".tmp", local_path)
    print("wrote %s" % local_path)
    print("  %s" % headline)

    if dry:
        print("DRY RUN — nothing published.")
        return out
    if "DASHBOARD_TOKEN" not in os.environ:
        print("no DASHBOARD_TOKEN set — local write only, skipping publish.")
        return out

    text = json.dumps(out, separators=(",", ":"), sort_keys=True)
    gh_put("coaching_roi.json", text, prev_sha,
           "Coaching ROI " + NOW.strftime("%Y-%m-%d %H:%M:%S"))
    print("published coaching_roi.json to %s" % PUB_REPO)
    return out


if __name__ == "__main__":
    main()
