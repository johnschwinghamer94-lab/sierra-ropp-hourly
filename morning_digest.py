#!/usr/bin/env python3
"""Morning Digest engine — one daily briefing covering BOTH departments
(ROPP/SILO sales and Service), published as digest.json for a dedicated
dashboard tab.

WHY THIS EXISTS: John currently learns what happened by opening several
dashboard tabs and reading numbers. This engine reads what has ALREADY been
published by the other engines (no ServiceTitan calls — cheap and fast) and
assembles: what happened yesterday, what's at risk today, and who to coach.
It is a dashboard tab, not an email.

Sources (all already-published artifacts, read-only):
  raw.githubusercontent.com/johnschwinghamer94-lab/sierra-ropp-dashboard/main/
    servicedata.json, servicecalls.json, servicecalls_history.json,
    servicefeed.json, service_coaching.json, coaching_outcomes.json,
    hourly.json, livefeed.json, coaching.json, health.json
  raw.githubusercontent.com/johnschwinghamer94-lab/sierra-ropp-hourly/main/
    tgl_truth/conv.json

EVERY read tolerates 404 / parse failure: a missing source degrades only the
section(s) that depend on it to null with a stated reason. This engine must
never crash and must never silently show zeros for missing data.

Output: digest.json — written locally (this repo's root) AND published to the
dashboard repo via the same GitHub contents-API helper (fetch-sha-then-PUT)
used by service_deck_refresh.py / health_check.py / coaching_outcomes.py.

Usage:
  python morning_digest.py            # build + publish (needs DASHBOARD_TOKEN)
  python morning_digest.py --dry      # build + print, publish nothing
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime.now(TZ)

DASH_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
ENGINE_REPO = "johnschwinghamer94-lab/sierra-ropp-hourly"
DASH_RAW = "https://raw.githubusercontent.com/" + DASH_REPO + "/main/"
ENGINE_RAW = "https://raw.githubusercontent.com/" + ENGINE_REPO + "/main/"
DASH_API = "https://api.github.com/repos/" + DASH_REPO + "/contents/"

UA = {"User-Agent": "sierra-morning-digest"}

MONTH_TARGET = 755333  # August 2026 — also present in servicedata.json's
                        # budget block; that live figure is preferred when
                        # available, this is only the doc-stated fallback.


# ── fetch helpers — every one degrades to None on any failure, never raises ─
def raw_json(base, path):
    req = urllib.request.Request(base + path, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} fetching {path}"
    except Exception as e:
        return None, f"could not fetch/parse {path}: {e}"


def dash_json(path):
    return raw_json(DASH_RAW, path)


def engine_json(path):
    return raw_json(ENGINE_RAW, path)


# ── business-day helpers ─────────────────────────────────────────────────
def business_day_str(d):
    return d.strftime("%A %-m/%-d") if os.name != "nt" else d.strftime("%A %m/%d").replace(" 0", " ")


def prev_business_day(d):
    d = d - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d - dt.timedelta(days=1)
    return d


def pct_delta(cur, prior):
    if cur is None or prior is None:
        return None
    return round(cur - prior, 1)


def dollar_delta(cur, prior):
    if cur is None or prior is None:
        return None
    return round(cur - prior)


# ── section builders ─────────────────────────────────────────────────────
def servicedata_bucket_date(sd):
    """The calendar date servicedata.json's live 'today' blocks actually describe,
    taken from its own 'updated' timestamp — or None if it can't be established.
    Any consumer of sd['today'] / sd['techs']['today'] must check this first: the
    bucket carries no date of its own, so it is only ever safe to attribute to
    the day this returns."""
    if not sd:
        return None
    updated = sd.get("updated")
    if not updated:
        return None
    try:
        return dt.datetime.fromisoformat(updated).astimezone(TZ).date().isoformat()
    except Exception:
        return None


def build_yesterday_service(sd, sd_err, target_date_iso):
    """servicedata.json's 'today' bucket is only usable as 'yesterday' data
    if its own 'updated' timestamp's calendar date == target business day
    (i.e. the digest is running the next morning before servicedata.py has
    rolled to a new day). If the bucket is NOT dated to the target business
    day, this engine has no other genuine closed-day source for Service
    figures (servicecalls.json/servicefeed.json don't carry revenue/close
    rate) — so every field is reported null rather than letting a live
    partial day masquerade as a confirmed closed day. servicedata.json also
    does not retain a second day of revenue/close-rate history, so
    vsPriorDay deltas are always null — reported with a stated reason rather
    than guessed."""
    notes = []
    if sd is None:
        return None, [f"servicedata.json unavailable: {sd_err}"]

    upd_date = servicedata_bucket_date(sd)

    today_blk = sd.get("today") or {}
    if upd_date != target_date_iso:
        notes.append(
            f"servicedata.json's 'today' bucket is dated {upd_date or 'unknown'}, "
            f"not the target business day {target_date_iso} — Service Pulse had not rolled to a "
            "new day (or is ahead of it) at digest run time, and this engine has no other closed-day "
            "Service revenue/close-rate source, so service.yesterday is null rather than presenting "
            "a live partial day as a confirmed closed day."
        )
        result = {
            "revenue": None, "calls": None, "closeRate": None, "avgTicket": None,
            "membershipsSold": None, "membershipOfferRate": None, "vsPriorDay": None,
        }
        return result, notes

    result = {
        "revenue": today_blk.get("revenue"),
        "calls": today_blk.get("opps"),
        "closeRate": today_blk.get("closeRate"),
        "avgTicket": today_blk.get("avgTicket"),
        "membershipsSold": today_blk.get("membershipsSold"),
        "membershipOfferRate": today_blk.get("membershipOfferRate"),
        "vsPriorDay": None,
    }
    notes.append(
        "vsPriorDay is null for Service: none of this engine's allowed sources "
        "(servicedata.json / servicecalls.json / servicefeed.json) retain a second day of "
        "per-day revenue/close-rate history — only the current live day is exposed."
    )
    return result, notes


def build_yesterday_silo(hourly, hourly_err, conv, conv_err, target_date_iso):
    """SILO yesterday figures must come from a genuine CLOSED day, never a
    live partial one. hourly.json only ever exposes a live 'today' bucket —
    it is usable ONLY when its own 'date' field equals the target business
    day (i.e. the digest ran before hourly.py rolled to the next day).
    Otherwise, calls/tgls/rate are sourced from tgl_truth/conv.json's
    per-tech day record for the target date (aggregated across techs), which
    is the ServiceTitan-derived record of a closed day. Revenue has no
    closed-day source in conv.json, so it is null whenever hourly.json can't
    supply it. If neither source can supply the target day, every field is
    null with a note — never a fabricated/live zero."""
    notes = []
    result = {"calls": None, "tgls": None, "rate": None, "revenue": None, "vsPriorDay": None}

    hourly_usable = False
    if hourly is None:
        notes.append(f"hourly.json unavailable: {hourly_err}")
    else:
        h_date = hourly.get("date")
        if h_date == target_date_iso:
            hourly_usable = True
            today_blk = hourly.get("today") or {}
            result["calls"] = today_blk.get("calls")
            result["tgls"] = today_blk.get("tgls")
            result["rate"] = today_blk.get("rate")
            result["revenue"] = today_blk.get("rev")
        else:
            notes.append(
                f"hourly.json is dated {h_date}, not the target business day {target_date_iso} — "
                "it only ever exposes a live 'today' bucket, so it cannot be used for SILO yesterday "
                "figures; falling back to tgl_truth/conv.json for a genuine closed-day record."
            )

    if conv is None:
        notes.append(f"tgl_truth/conv.json unavailable: {conv_err}")
    else:
        days = conv.get("days") or {}
        cur = days.get(target_date_iso)
        prior_date = prev_business_day(dt.date.fromisoformat(target_date_iso)).isoformat()
        prior = days.get(prior_date)
        if not hourly_usable:
            if cur:
                cur_calls = sum((v.get("calls") or 0) for v in cur.values())
                cur_tgls = sum((v.get("tgls") or 0) for v in cur.values())
                if cur_calls:
                    result["calls"] = cur_calls
                    result["tgls"] = cur_tgls
                    result["rate"] = round(cur_tgls / cur_calls * 100, 1)
                    notes.append(
                        f"SILO yesterday calls/tgls/rate sourced from tgl_truth/conv.json for "
                        f"{target_date_iso} (closed-day record) — hourly.json could not supply the "
                        "target business day."
                    )
                else:
                    notes.append(
                        f"SILO yesterday calls/tgls/rate are null — tgl_truth/conv.json has no "
                        f"per-tech data yet for {target_date_iso} (empty day record), and hourly.json "
                        "could not supply the target business day either."
                    )
            else:
                notes.append(
                    f"SILO yesterday calls/tgls/rate are null — tgl_truth/conv.json has no day record "
                    f"for {target_date_iso}, and hourly.json could not supply the target business day either."
                )
            notes.append(
                "SILO yesterday.revenue is null — tgl_truth/conv.json does not carry revenue and "
                "hourly.json could not supply the target business day."
            )
        if cur and prior:
            prior_calls = sum((v.get("calls") or 0) for v in prior.values())
            prior_tgls = sum((v.get("tgls") or 0) for v in prior.values())
            prior_rate = round(prior_tgls / prior_calls * 100, 1) if prior_calls else None
            cur_calls = sum((v.get("calls") or 0) for v in cur.values())
            cur_tgls = sum((v.get("tgls") or 0) for v in cur.values())
            cur_rate = round(cur_tgls / cur_calls * 100, 1) if cur_calls else None
            result["vsPriorDay"] = {
                "calls": dollar_delta(cur_calls, prior_calls),
                "tgls": dollar_delta(cur_tgls, prior_tgls),
                "rate": pct_delta(cur_rate, prior_rate),
                "revenue": None,
            }
            notes.append("SILO vsPriorDay.revenue is null — tgl_truth/conv.json does not carry revenue.")
        else:
            notes.append(
                f"SILO vsPriorDay unavailable — tgl_truth/conv.json has no per-tech day data for "
                f"{target_date_iso} and/or the prior business day {prior_date}."
            )

    return result, notes


# health.json 'checks[].key' -> which deck it belongs to. Keys that are
# genuinely shared infrastructure (neither deck exclusively) map to "both";
# everything else is judged by what the check actually monitors.
HEALTH_CHECK_DEPT = {
    "livefeed": "silo",           # SILO live feed
    "servicefeed": "service",     # Service live feed
    "hourly": "silo",             # ROPP hourly capture (SILO)
    "servicedata": "service",     # Service Pulse data
    "servicecalls": "service",    # Calls board (Service)
    "scorecards": "silo",         # SILO live scorecards
    "servicecards": "service",    # Service live scorecards
    "coaching": "silo",           # SILO daily plans
    "service_coaching": "service",  # Service daily plans
    "tgl_conv": "silo",           # ServiceTitan TGL truth — feeds SILO flip-rate/coach data
    "tgl_truth_daily": "silo",    # TGL truth daily files — same lineage as tgl_conv
    "deck_blob": "service",       # Service classic deck blob
}


def build_risks(sd, servicecalls, sc_hist, feed, health, sd_budget_target, notes):
    risks = []

    # ── board fill vs trailing weekday average ──────────────────────────
    if servicecalls and sc_hist:
        try:
            # latest count per calendar date seen in the history snapshots
            latest_by_date = {}
            for snap in sc_hist:
                for date_iso, count in (snap.get("counts") or {}).items():
                    latest_by_date[date_iso] = count  # later snapshots overwrite earlier
            # group PAST dates (before today) by weekday for a trailing average
            today = NOW.date()
            by_weekday = {}
            for date_iso, count in latest_by_date.items():
                try:
                    d = dt.date.fromisoformat(date_iso)
                except Exception:
                    continue
                if d < today:
                    by_weekday.setdefault(d.weekday(), []).append(count)

            any_weekday_history = False
            for day in (servicecalls.get("days") or [])[:5]:
                date_iso = day.get("date")
                total = day.get("total")
                if not date_iso or total is None:
                    continue
                try:
                    d = dt.date.fromisoformat(date_iso)
                except Exception:
                    continue
                samples = by_weekday.get(d.weekday(), [])
                if len(samples) < 2:
                    continue  # not enough trailing history to call this a trend yet
                any_weekday_history = True
                avg = sum(samples) / len(samples)
                if avg > 0 and total < avg * 0.8:
                    gap = round(avg - total)
                    days_out = (d - today).days
                    risks.append({
                        "severity": "high" if total < avg * 0.65 else "medium",
                        "area": "board",
                        "dept": "service",
                        "text": (f"{d.strftime('%A')} {d.strftime('%-m/%-d')} has {total} booked vs "
                                 f"~{round(avg)} typical for that weekday — {gap} short with "
                                 f"{days_out} day{'s' if days_out != 1 else ''} to fill."),
                        "metric": {"date": date_iso, "booked": total, "typical": round(avg, 1), "gap": gap},
                    })
            if not any_weekday_history:
                notes.append(
                    "board-fill trend risk skipped — servicecalls_history.json does not yet contain "
                    "2+ trailing samples for any of the same weekdays as the next 5 business days "
                    "(the snapshot history is too new/short to compute a trailing weekday average)."
                )
        except Exception as e:
            notes.append(f"board-fill risk check failed: {e}")
    else:
        notes.append("board-fill risk skipped — servicecalls.json or servicecalls_history.json unavailable.")

    # ── unassigned appointments still unassigned ─────────────────────────
    if servicecalls:
        for day in (servicecalls.get("days") or []):
            unassigned = day.get("unassigned")
            if unassigned:
                risks.append({
                    "severity": "high" if unassigned >= 5 else "medium",
                    "area": "board",
                    "dept": "service",
                    "text": f"{day.get('label', day.get('date'))} has {unassigned} unassigned appointment"
                            f"{'s' if unassigned != 1 else ''} still on the board.",
                    "metric": {"date": day.get("date"), "unassigned": unassigned},
                })
    else:
        notes.append("unassigned-appointment risk skipped — servicecalls.json unavailable.")

    # ── recording compliance ────────────────────────────────────────────
    co, co_err = health  # (data, err) tuple passed in from caller for coaching_outcomes
    if co:
        compliance = co.get("compliance") or {}
        dept = compliance.get("dept") or {}
        dept_pct = dept.get("pct")
        if dept_pct is not None and dept_pct < 90:
            risks.append({
                "severity": "high" if dept_pct < 75 else "medium",
                "area": "recording",
                "dept": "service",
                "text": f"Service recording compliance is {dept_pct}% over the last 7 days — below the 90% target.",
                "metric": {"deptPct": dept_pct},
            })
        for tech, t in (compliance.get("techs") or {}).items():
            if t.get("eligible") and t.get("pct") == 0:
                risks.append({
                    "severity": "high",
                    "area": "recording",
                    "dept": "service",
                    "text": f"{tech} recorded 0% of {t.get('eligible')} eligible jobs over the last 7 days.",
                    "metric": {"tech": tech, "eligible": t.get("eligible"), "pct": 0},
                })
    elif co_err:
        notes.append(f"recording-compliance risk skipped — {co_err}")

    # ── membership offer rate ───────────────────────────────────────────
    # STANDARD (John, 2026-08-05): every eligible call gets a membership offer,
    # so 90%+ is the bar; 70-89 medium, under 70 high. Measured WEEK TO DATE,
    # never today's partial day — at 8 AM barely anything has closed and the
    # old rule fired "high" on a 0% that only meant "the day just started".
    if sd:
        wtd_blk = sd.get("wtd") or {}
        offer_rate = wtd_blk.get("membershipOfferRate")
        offered = wtd_blk.get("membershipOffered")
        elig = wtd_blk.get("membershipOfferJobs")
        if offer_rate is not None and elig and offer_rate < 90:
            risks.append({
                "severity": "high" if offer_rate < 70 else "medium",
                "area": "membership",
                "dept": "service",
                "text": (f"Membership offered on {offered} of {elig} eligible calls this week "
                         f"({offer_rate}%) — the standard is every eligible call."),
                "metric": {"offerRate": offer_rate, "offered": offered, "eligible": elig},
            })
        # Per-tech: week to date, and only with enough volume to mean something.
        # A 1-job sample is a coin flip, not a finding.
        MIN_JOBS = 3
        for t in (sd.get("techs", {}).get("wtd") or []):
            jobs = t.get("membershipOfferJobs") or 0
            rate = t.get("membershipOfferRate")
            if jobs >= MIN_JOBS and rate == 0:
                risks.append({
                    "severity": "high",
                    "area": "membership",
                    "dept": "service",
                    "text": (f"{t.get('name')} has not offered a membership once this week "
                             f"— {jobs} eligible calls."),
                    "metric": {"tech": t.get("name"), "eligibleCalls": jobs},
                })
    else:
        notes.append("membership-offer risk skipped — servicedata.json unavailable.")

    # ── pipeline health dead/warn ────────────────────────────────────────
    hjson, hjson_err = HEALTH_JSON_HOLDER["val"]
    if hjson:
        for c in (hjson.get("checks") or []):
            if c.get("status") in ("dead", "warn"):
                risks.append({
                    "severity": "high" if c["status"] == "dead" else "low",
                    "area": "pipeline",
                    "dept": HEALTH_CHECK_DEPT.get(c.get("key"), "both"),
                    "text": f"{c.get('label')} is {c['status']}: {c.get('note')}",
                    "metric": {"key": c.get("key"), "status": c.get("status")},
                })
    else:
        notes.append(f"pipeline health.json risk skipped — {hjson_err}")

    # ── month pace vs budget target ─────────────────────────────────────
    if sd:
        budget = sd.get("budget") or {}
        target = budget.get("revenueTarget")
        if not target:
            # MONTH_TARGET is an August-2026 constant. Substituting it silently
            # publishes an ahead/behind-pace verdict measured against a target that
            # may belong to a different month entirely.
            target = MONTH_TARGET
            notes.append(
                f"month-pace risk used the hardcoded fallback target ${MONTH_TARGET:,} "
                f"(documented for August 2026) because servicedata.json's budget block "
                f"carried no revenueTarget — the ahead/behind verdict is only meaningful "
                f"if that is still the current month's target."
            )
        working_days = budget.get("workingDays")
        remaining = budget.get("remainingWorkingDays")
        mtd_actual = budget.get("monthActualSoFar")
        if target and working_days and remaining is not None and mtd_actual is not None:
            elapsed_days = working_days - remaining
            if elapsed_days > 0:
                expected_share = target * (elapsed_days / working_days)
                gap = round(mtd_actual - expected_share)
                if gap < 0:
                    risks.append({
                        "severity": "high" if gap < -0.15 * expected_share else "medium",
                        "area": "pipeline",
                        "dept": "service",
                        "text": (f"August is behind pace: ${mtd_actual:,.0f} MTD vs ~${expected_share:,.0f} "
                                 f"expected through {elapsed_days} of {working_days} working days "
                                 f"(${abs(gap):,.0f} short)."),
                        "metric": {"mtdActual": mtd_actual, "expected": round(expected_share), "gap": gap},
                    })
                else:
                    risks.append({
                        "severity": "low",
                        "area": "pipeline",
                        "dept": "service",
                        "text": (f"August is ahead of pace: ${mtd_actual:,.0f} MTD vs ~${expected_share:,.0f} "
                                 f"expected through {elapsed_days} of {working_days} working days "
                                 f"(+${gap:,.0f})."),
                        "metric": {"mtdActual": mtd_actual, "expected": round(expected_share), "gap": gap},
                    })
        else:
            notes.append("month-pace risk skipped — servicedata.json budget block missing required fields.")
    else:
        notes.append("month-pace risk skipped — servicedata.json unavailable.")

    order = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda r: order.get(r["severity"], 3))
    return risks


HEALTH_JSON_HOLDER = {"val": (None, None)}


def build_coach_focus(sc, sc_err, co, co_err, notes):
    """Rank Service techs by a composite of: count of failed critical flags
    (service_coaching.json reps[].critical), membership offer rate, recording
    compliance (coaching_outcomes.json), options-per-opp. Give each a
    one-sentence reason + concrete evidence + plan link where available."""
    if sc is None:
        notes.append(f"coachFocus unavailable — service_coaching.json unavailable: {sc_err}")
        return []

    compliance_techs = {}
    if co:
        compliance_techs = (co.get("compliance") or {}).get("techs") or {}
    elif co_err:
        notes.append(f"coachFocus recording-compliance evidence skipped — coaching_outcomes.json: {co_err}")

    scored = []
    for rep in sc.get("reps") or []:
        name = rep.get("name")
        critical = rep.get("critical") or {}
        failed = [k for k, v in critical.items() if v is True]
        n_failed = len(failed)
        comp = compliance_techs.get(name, {})
        comp_pct = comp.get("pct")
        score = n_failed * 10
        if comp_pct is not None and comp_pct < 90:
            score += (90 - comp_pct) / 10
        if score == 0 and not failed:
            continue
        evidence_bits = []
        if failed:
            evidence_bits.append(f"failed critical flags: {', '.join(failed)}")
        if rep.get("closeRate"):
            evidence_bits.append(f"close rate {rep['closeRate']}")
        if comp_pct is not None:
            evidence_bits.append(f"recording compliance {comp_pct}% (7-day)")
        scored.append({
            "tech": name,
            "reason": rep.get("headlineGap") or "Coaching plan flagged critical gaps.",
            "evidence": "; ".join(evidence_bits) if evidence_bits else "See coaching plan for detail.",
            "planLink": rep.get("plan"),
            "_score": score,
        })

    scored.sort(key=lambda r: r["_score"], reverse=True)
    top = scored[:5]
    for r in top:
        del r["_score"]
    return top


def build_wins(sd, sd_bucket_date, target_date_iso, sc, co, notes):
    """Service wins for the business day being briefed.

    servicedata.json's techs.today is a LIVE bucket with no date of its own —
    it is only ever safe to use once servicedata_bucket_date(sd) (the SAME
    dated-source helper build_yesterday_service() uses for its aggregate
    figures) has established what calendar day it actually describes. In
    practice servicedata.py's daily full rebuild (5:35 AM PT) runs before this
    digest (6:30 AM PT), so the bucket is essentially always already stamped
    with the current day rather than the prior closed business day by the
    time this runs — requiring an exact match to target_date_iso made this
    section skip on every run. Rather than force it to describe a day it
    never will, or fabricate/mislabel which day it covers, this credits the
    bucket for whatever day servicedata_bucket_date proves it describes and
    labels the win with THAT real date — never a forced/hardcoded one. Only
    skip (with a note) when the dated source genuinely can't establish a day
    at all."""
    wins = []
    if sd is None:
        notes.append("wins (service volume/close) skipped — servicedata.json unavailable.")
    elif sd_bucket_date is None:
        notes.append(
            "service wins skipped — servicedata.json's 'updated' timestamp could not be "
            "parsed, so the day its live 'today' tech bucket describes cannot be established."
        )
    else:
        techs_today = sd.get("techs", {}).get("today") or []
        volume_techs = [t for t in techs_today if (t.get("opps") or 0) >= 2]
        if volume_techs:
            best_offer = max(volume_techs, key=lambda t: t.get("membershipOfferRate") or -1)
            if (best_offer.get("membershipOfferRate") or 0) >= 80:
                wins.append({
                    "tech": best_offer["name"],
                    "text": f"{best_offer['name']} offered a membership on {best_offer['membershipOfferRate']}% "
                            f"of {best_offer.get('membershipOfferJobs')} jobs on {sd_bucket_date}.",
                })
            best_close = max(volume_techs, key=lambda t: t.get("closeRate") or -1)
            if (best_close.get("closeRate") or 0) >= 70:
                wins.append({
                    "tech": best_close["name"],
                    "text": f"{best_close['name']} closed {best_close['closeRate']}% on {best_close.get('opps')} "
                            f"opportunities on {sd_bucket_date}.",
                })

    if co:
        for o in co.get("outcomes") or []:
            if o.get("verdict") == "improved":
                wins.append({
                    "tech": o["tech"],
                    "text": f"{o['tech']}'s {o['metric']} improved from {o['before']} to {o['after']} "
                            f"since the {o['date']} coaching plan.",
                })
    return wins[:3]


def build_silo_coach_focus(coaching_silo, coaching_silo_err, conv, conv_err, target_date_iso, notes):
    """Rank SILO reps by genuine weakness using coaching.json's own bands/
    critical/headlineGap/closeRate, cross-checked against real flip rates
    (calls -> TGLs) from tgl_truth/conv.json where available. Plan link comes
    straight from coaching.json's per-rep 'plan' field (coaching/plans/<date>/
    <First_Last>.html) when present."""
    if coaching_silo is None:
        notes.append(f"siloCoachFocus unavailable — coaching.json unavailable: {coaching_silo_err}")
        return []

    flip_by_tech = {}
    if conv:
        days = conv.get("days") or {}
        cur = days.get(target_date_iso) or {}
        for tech, v in cur.items():
            calls = v.get("calls") or 0
            tgls = v.get("tgls") or 0
            if calls >= 3:
                flip_by_tech[tech] = {"calls": calls, "tgls": tgls, "rate": round(tgls / calls * 100, 1)}
    elif conv_err:
        notes.append(f"siloCoachFocus flip-rate evidence skipped — tgl_truth/conv.json: {conv_err}")

    band_weight = {"Weak": 3, "Moderate": 2, "Solid": 1, "Strong on wins": 0, "Strong": 0}
    scored = []
    for rep in coaching_silo.get("reps") or []:
        name = rep.get("name")
        critical = rep.get("critical") or {}
        failed = [k for k, v in critical.items() if v is True]
        bands = rep.get("bands") or {}
        weak_bands = [k for k, v in bands.items() if band_weight.get(v, 0) >= 2]
        score = len(failed) * 10 + sum(band_weight.get(v, 0) for v in bands.values())
        if score == 0 and not failed:
            continue
        evidence_bits = []
        if failed:
            evidence_bits.append(f"failed critical flags: {', '.join(failed)}")
        if weak_bands:
            evidence_bits.append(f"weak/moderate bands: {', '.join(weak_bands)}")
        if rep.get("closeRate"):
            evidence_bits.append(f"close rate {rep['closeRate']}")
        flip = flip_by_tech.get(name)
        if flip:
            evidence_bits.append(f"flip rate {flip['rate']}% ({flip['tgls']}/{flip['calls']} calls)")
        scored.append({
            "tech": name,
            "reason": rep.get("headlineGap") or "Coaching plan flagged critical gaps.",
            "evidence": "; ".join(evidence_bits) if evidence_bits else "See coaching plan for detail.",
            "planLink": rep.get("plan"),
            "_score": score,
        })

    scored.sort(key=lambda r: r["_score"], reverse=True)
    top = scored[:5]
    for r in top:
        del r["_score"]
    return top


def build_silo_wins(coaching_silo, conv, target_date_iso, notes):
    """SILO wins: standout flip rates (real volume via tgl_truth/conv.json)
    or improved coaching bands (Strong / Strong on wins) with real close-rate
    evidence from coaching.json. Empty array if nothing honestly qualifies —
    never fabricated."""
    wins = []
    if conv:
        days = conv.get("days") or {}
        cur = days.get(target_date_iso) or {}
        candidates = []
        for tech, v in cur.items():
            calls = v.get("calls") or 0
            tgls = v.get("tgls") or 0
            if calls >= 5:
                rate = round(tgls / calls * 100, 1)
                candidates.append((tech, calls, tgls, rate))
        if candidates:
            best = max(candidates, key=lambda c: c[3])
            if best[3] >= 40:
                wins.append({
                    "tech": best[0],
                    "text": f"{best[0]} flipped {best[2]} of {best[1]} calls to TGLs ({best[3]}%) on {target_date_iso}.",
                })
    else:
        notes.append("siloWins flip-rate check skipped — tgl_truth/conv.json unavailable.")

    if coaching_silo:
        for rep in coaching_silo.get("reps") or []:
            bands = rep.get("bands") or {}
            if bands and all(v in ("Strong", "Strong on wins") for v in bands.values()) and rep.get("calls", 0) and rep.get("calls") >= 2:
                wins.append({
                    "tech": rep.get("name"),
                    "text": f"{rep.get('name')} graded Strong across every stage on {rep.get('calls')} calls "
                            f"({rep.get('closeRate')}).",
                })

    return wins[:3]


def build_health_summary():
    hjson, err = HEALTH_JSON_HOLDER["val"]
    if not hjson:
        return {"worst": None, "dead": [], "warn": []}, [f"health summary unavailable — {err}"]
    dead = [c["label"] for c in hjson.get("checks", []) if c.get("status") == "dead"]
    warn = [c["label"] for c in hjson.get("checks", []) if c.get("status") == "warn"]
    return {"worst": hjson.get("worst"), "dead": dead, "warn": warn}, []


# ── publish ──────────────────────────────────────────────────────────────
def _gh_headers(extra=None):
    h = {"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
         "Accept": "application/vnd.github+json", "User-Agent": "morning-digest"}
    if extra:
        h.update(extra)
    return h


def gh_fetch_sha(path):
    req = urllib.request.Request(DASH_API + path, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def gh_put(path, text, sha, msg):
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(DASH_API + path, data=json.dumps(body).encode(), method="PUT",
                                  headers=_gh_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read())


def main():
    dry = "--dry" in sys.argv
    notes = []

    # Business day being briefed: most recent business day strictly before
    # today (i.e. "yesterday" — Fri if today is Mon/Sat/Sun).
    target_date = prev_business_day(NOW.date())
    target_date_iso = target_date.isoformat()

    sd, sd_err = dash_json("servicedata.json")
    servicecalls, sc_calls_err = dash_json("servicecalls.json")
    sc_hist, sc_hist_err = dash_json("servicecalls_history.json")
    feed, feed_err = dash_json("servicefeed.json")
    sc_coaching, sc_coaching_err = dash_json("service_coaching.json")
    co, co_err = dash_json("coaching_outcomes.json")
    hourly, hourly_err = dash_json("hourly.json")
    livefeed, livefeed_err = dash_json("livefeed.json")
    coaching_silo, coaching_silo_err = dash_json("coaching.json")
    hjson, hjson_err = dash_json("health.json")
    conv, conv_err = engine_json("tgl_truth/conv.json")

    HEALTH_JSON_HOLDER["val"] = (hjson, hjson_err)

    for label, err in [
        ("servicedata.json", sd_err if sd is None else None),
        ("servicecalls.json", sc_calls_err if servicecalls is None else None),
        ("servicecalls_history.json", sc_hist_err if sc_hist is None else None),
        ("servicefeed.json", feed_err if feed is None else None),
        ("service_coaching.json", sc_coaching_err if sc_coaching is None else None),
        ("coaching_outcomes.json", co_err if co is None else None),
        ("hourly.json", hourly_err if hourly is None else None),
        ("livefeed.json", livefeed_err if livefeed is None else None),
        ("coaching.json", coaching_silo_err if coaching_silo is None else None),
        ("health.json", hjson_err if hjson is None else None),
        ("tgl_truth/conv.json", conv_err if conv is None else None),
    ]:
        if err:
            notes.append(f"SOURCE UNAVAILABLE: {label} — {err}")

    yest_service, ys_notes = build_yesterday_service(sd, sd_err, target_date_iso)
    yest_silo, yl_notes = build_yesterday_silo(hourly, hourly_err, conv, conv_err, target_date_iso)
    notes.extend(ys_notes)
    notes.extend(yl_notes)

    sd_day = servicedata_bucket_date(sd)
    risks = build_risks(sd, servicecalls, sc_hist, feed, (co, co_err), None, notes)
    service_coach_focus = build_coach_focus(sc_coaching, sc_coaching_err, co, co_err, notes)
    service_wins = build_wins(sd, sd_day, target_date_iso, sc_coaching, co, notes)
    silo_coach_focus = build_silo_coach_focus(coaching_silo, coaching_silo_err, conv, conv_err, target_date_iso, notes)
    silo_wins = build_silo_wins(coaching_silo, conv, target_date_iso, notes)
    health_summary, h_notes = build_health_summary()
    notes.extend(h_notes)
    notes.append(
        "DEPRECATED: top-level 'coachFocus'/'wins' are aliases for "
        "'serviceCoachFocus'/'serviceWins' kept for one release for backward "
        "compatibility — new consumers should read the dept-specific fields "
        "(serviceCoachFocus/serviceWins/siloCoachFocus/siloWins) directly."
    )

    gen = NOW
    payload = {
        "generated": gen.isoformat(),
        "generatedMs": int(gen.timestamp() * 1000),
        "forDate": target_date_iso,
        "yesterday": {"service": yest_service, "silo": yest_silo},
        "risks": risks,
        "serviceCoachFocus": service_coach_focus,
        "serviceWins": service_wins,
        "siloCoachFocus": silo_coach_focus,
        "siloWins": silo_wins,
        # deprecated aliases — mirror service arrays, kept for one release
        "coachFocus": service_coach_focus,
        "wins": service_wins,
        "health": health_summary,
        "notes": notes,
    }
    payload_text = json.dumps(payload, indent=2, sort_keys=False)

    local_path = os.path.join(HERE, "digest.json")
    with open(local_path + ".tmp", "w") as f:
        f.write(payload_text)
    os.replace(local_path + ".tmp", local_path)
    print(f"wrote {local_path}")

    print(payload_text)

    if dry:
        print("DRY RUN — nothing published.")
        return

    if "DASHBOARD_TOKEN" not in os.environ:
        print("no DASHBOARD_TOKEN set — local write only, skipping publish.")
        return

    sha = gh_fetch_sha("digest.json")
    gh_put("digest.json", payload_text, sha, "Morning digest " + gen.strftime("%Y-%m-%d %H:%M PT"))
    print("published digest.json to " + DASH_REPO)


if __name__ == "__main__":
    main()
