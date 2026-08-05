#!/usr/bin/env python3
"""Per-tech, per-day TGL conversion ground truth for the SILO coaching pipeline —
mirrors the DASHBOARD's own arithmetic (UPDATE_DASHBOARD.parse_all) so coaching
plans quote the same close-rate numbers John sees on the dashboard, instead of a
rate derived only from graded Siro calls.

For each of the trailing 40 PT dates (today back 39 days), counts, per tech:
  * calls  — Revenue_By_JobType.xlsx rows grouped by "Assigned Technicians",
             kept only if the job is in the ROPP-tag-cleaned set (_rev_clean_set()),
             tech = resolve(row[7]) or resolve(group), date = to_date(row[4])
  * tgls   — ROPP_TGLs_Created.xlsx rows grouped by "Assigned Technicians",
             tech = resolve(row[3]) or resolve(group), date = to_date(row[5])
This is exactly the row/column logic parse_all() uses to build dashboard
mtd/ytd rates — see UPDATE_DASHBOARD.py ~lines 289-322.

No rates are computed here; consumers (the coaching-plan prompt) divide
tgls/calls themselves per day/tech so they control rounding and fallback.

Writes tgl_truth/conv.json:
    {"generated": "<iso now PT>",
     "method": "dashboard parse_all: TGLs Created per creating tech / Revenue-report
                 calls ran (ROPP-tag cleaned)",
     "days": {"<iso date>": {"<tech>": {"calls": N, "tgls": N}, ...}, ...}}

With ROPP_SOURCE=api (the daily workflow's mode), load_rows reads from the
cache/*.json already fetched by cloud_fetch.py earlier in the run (via
ropp_live.load_rows monkeypatching UPDATE_DASHBOARD.load_rows) — no extra
API calls are made here.
"""
import json, os, pathlib, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception as _e:
    # Every day key in conv.json is a PACIFIC calendar date. Falling back to UTC
    # silently shifts the boundary 7-8h, so evening calls land on the wrong day
    # and per-day flip rates quietly disagree with the dashboard. Fail loud.
    raise SystemExit("tgl_conv: America/Los_Angeles timezone unavailable (%s) — refusing "
                     "to bucket days in UTC, which would misdate every evening call." % _e)

import UPDATE_DASHBOARD as U

if os.environ.get("ROPP_SOURCE") == "api":
    import ropp_live
    U.load_rows = ropp_live.load_rows
    print("tgl_conv: data source ServiceTitan Reporting API (ropp_live)")

load_rows = U.load_rows
iter_grouped = U.iter_grouped
resolve = U.resolve
to_date = U.to_date
jobkey = U.jobkey
_rev_clean_set = U._rev_clean_set

OUT_DIR = HERE / "tgl_truth"


def now_pt():
    return datetime.now(PT) if PT else datetime.now(timezone.utc)


def main():
    today_pt = now_pt().date()
    trailing_dates = [today_pt - timedelta(days=i) for i in range(39, -1, -1)]  # oldest -> newest, 40 dates
    date_set = {d.isoformat() for d in trailing_dates}

    days = {d.isoformat(): defaultdict(lambda: {"calls": 0, "tgls": 0}) for d in trailing_dates}

    for grp, r in iter_grouped(load_rows("ROPP_TGLs_Created.xlsx"), "Assigned Technicians", 1):
        tech = resolve(r[3]) or resolve(grp)
        d = to_date(r[5])
        if not tech or d is None:
            continue
        dkey = d.isoformat()
        if dkey not in date_set:
            continue
        days[dkey][tech]["tgls"] += 1

    clean = _rev_clean_set()
    for grp, r in iter_grouped(load_rows("Revenue_By_JobType.xlsx"), "Assigned Technicians", 3):
        if jobkey(r[3]) not in clean:
            continue
        tech = resolve(r[7]) or resolve(grp)
        d = to_date(r[4])
        if not tech or d is None:
            continue
        dkey = d.isoformat()
        if dkey not in date_set:
            continue
        days[dkey][tech]["calls"] += 1

    out_days = {dkey: dict(techs) for dkey, techs in days.items()}

    # A 40-day window in which no tech ran a single call is not a slow month, it
    # is an empty input set (missing cache, failed report fetch). Writing it would
    # overwrite the good conv.json with all-zero days, and every consumer —
    # coaching plans, morning_digest's SILO section, siloWins — would then quote
    # 0 calls / 0 TGLs / 0% as fact. This is exactly how the headline coaching
    # metric froze for nine days. Refuse, keep the last good file, exit nonzero.
    total_calls = sum(t["calls"] for techs in out_days.values() for t in techs.values())
    total_tgls = sum(t["tgls"] for techs in out_days.values() for t in techs.values())
    if total_calls == 0 and total_tgls == 0:
        raise SystemExit(
            "tgl_conv: REFUSING TO WRITE conv.json — 0 calls and 0 TGLs across the whole "
            "%s..%s window. That is a failed/empty source read, not a real zero; the "
            "existing conv.json is left untouched. Check ROPP_TGLs_Created.xlsx and "
            "Revenue_By_JobType.xlsx (or the API cache when ROPP_SOURCE=api)."
            % (trailing_dates[0].isoformat(), trailing_dates[-1].isoformat()))

    OUT_DIR.mkdir(exist_ok=True)
    payload = {
        "generated": now_pt().isoformat(),
        "method": "dashboard parse_all: TGLs Created per creating tech / Revenue-report calls ran (ROPP-tag cleaned)",
        "days": out_days,
    }
    (OUT_DIR / "conv.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    all_techs = set()
    for techs in out_days.values():
        all_techs.update(techs.keys())
    print(f"tgl_conv: {trailing_dates[0].isoformat()}..{trailing_dates[-1].isoformat()} "
          f"({len(trailing_dates)} days) | {len(all_techs)} techs")


if __name__ == "__main__":
    main()
