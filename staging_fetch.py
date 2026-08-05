#!/usr/bin/env python3
"""Refresh the STAGING dashboard caches (CA Sales + HVAC Service) from the live
ServiceTitan Reporting API, with fully dynamic dates (no hardcoded year/month).

YTD  = Jan 1 of current year -> today (America/Los_Angeles)
MTD  = 1st of current month  -> today
monthly = one Field Conversion v1 run per elapsed month of the current year

Report IDs resolved by enumerating /reporting/v2/tenant/{tenant}/report-categories
and report-category/{cat}/reports (2026-07-29):
  CA Conversion        sold-by / 318700602
  Sales from Tech Leads       technician-dashboard / 216
  Sales from Marketing Leads  technician-dashboard / 219
  Field Conversion Report v1  technician / 328361546  (core Service metrics)
  Field Conversion Report     technician / 4829        (team labels only)
  Memberships Sold By Report  sold-by / 4814
  IAQ SOLD ...                technician / 623417405

Does NOT touch the ROPP cache files (estimate/revenue/scheduled/cancellations) —
those are refreshed by the separate prod pipeline every ~15 min.
"""
import json, os, sys, time, urllib.error, datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import st_client as st

TZ = ZoneInfo("America/Los_Angeles")


def call(fn, *a, **k):
    """Shared retry wrapper — st_client's _open already retries 429/5xx internally,
    but run_report calls go through api_post -> _open, so this is a thin no-op
    passthrough kept for symmetry with fetch_ropp_headline.py / ropp_live.py; the
    real backoff lives in st_client._open."""
    return fn(*a, **k)


def fetch_report(cat, rid, params, label):
    rows, page, fields = [], 1, None
    while True:
        r = call(st.run_report, cat, rid, params, page=page, page_size=5000)
        f = [x["name"] for x in r.get("fields", [])]
        if fields is None:
            fields = f
        rows += r.get("data", [])
        if not r.get("hasMore") or not r.get("data"):
            break
        page += 1
        time.sleep(1)
    print(f"  {label:28} -> {len(rows):5} rows")
    return fields, rows


def write_cache(name, fields, rows, extra=None):
    path = os.path.join(HERE, "cache", name + ".json")
    # A report that suddenly returns 0 rows (parameter/permission/report-id drift)
    # is silent here: the empty result overwrites a good cache, then service_live /
    # ca_live build a deck of $0 and 0% from it and it gets published as fact.
    # Regressing from data to no-data is never legitimate — refuse and keep the
    # last good cache. ALLOW_EMPTY_CACHE=1 overrides for a genuine empty period.
    if not rows and not os.environ.get("ALLOW_EMPTY_CACHE"):
        try:
            with open(path) as f:
                prev_rows = len(json.load(f).get("rows") or [])
        except (OSError, ValueError):
            prev_rows = 0
        if prev_rows:
            raise RuntimeError(
                "cache/%s.json: report returned 0 rows but the existing cache holds %d — "
                "refusing to overwrite good data with an empty result (that publishes a $0 "
                "deck). Check the report id/params. Set ALLOW_EMPTY_CACHE=1 to override."
                % (name, prev_rows))
        print(f"  WARN: {name} returned 0 rows and there was no prior cache — "
              "downstream decks will show zeros for it.")
    payload = {"fields": fields, "rows": rows}
    if extra:
        payload.update(extra)
    with open(path + ".tmp", "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def _date_range():
    today = dt.datetime.now(TZ).date()
    ytd_from = dt.date(today.year, 1, 1)
    mtd_from = dt.date(today.year, today.month, 1)
    return today, ytd_from, mtd_from


def fetch_ca(today, ytd_from, mtd_from):
    print("== CA Sales reports ==")
    ytd_p = [{"name": "From", "value": ytd_from.isoformat()}, {"name": "To", "value": today.isoformat()}]
    mtd_p = [{"name": "From", "value": mtd_from.isoformat()}, {"name": "To", "value": today.isoformat()}]

    f, r = fetch_report("sold-by", 318700602, ytd_p, "ca_conversion (YTD)")
    write_cache("ca_conversion", f, r)
    f, r = fetch_report("sold-by", 318700602, mtd_p, "ca_conversion (MTD)")
    write_cache("ca_conversion_mtd", f, r)

    f, r = fetch_report("technician-dashboard", 216, ytd_p, "ca_techleads (YTD)")
    write_cache("ca_techleads", f, r)
    f, r = fetch_report("technician-dashboard", 216, mtd_p, "ca_techleads (MTD)")
    write_cache("ca_techleads_mtd", f, r)

    f, r = fetch_report("technician-dashboard", 219, ytd_p, "ca_mktleads (YTD)")
    write_cache("ca_mktleads", f, r)
    f, r = fetch_report("technician-dashboard", 219, mtd_p, "ca_mktleads (MTD)")
    write_cache("ca_mktleads_mtd", f, r)


def fetch_service(today, ytd_from):
    print("== Service reports ==")
    ytd_p = [{"name": "From", "value": ytd_from.isoformat()}, {"name": "To", "value": today.isoformat()},
             {"name": "IncludeInactive", "value": False}]

    f, r = fetch_report("technician", 4829, ytd_p, "service_fieldconv (team labels)")
    write_cache("service_fieldconv", f, r)

    f, r = fetch_report("technician", 328361546, ytd_p, "service_v1 (FC v1 YTD)")
    write_cache("service_v1", f, r)

    memb_p = [{"name": "From", "value": ytd_from.isoformat()}, {"name": "To", "value": today.isoformat()}]
    f, r = fetch_report("sold-by", 4814, memb_p, "service_memberships")
    write_cache("service_memberships", f, r)

    iaq_p = [{"name": "DateType", "value": 1}, {"name": "From", "value": ytd_from.isoformat()},
             {"name": "To", "value": today.isoformat()}]
    f, r = fetch_report("technician", 623417405, iaq_p, "service_iaq")
    write_cache("service_iaq", f, r)


MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def fetch_service_monthly(today):
    """One Field Conversion v1 run per elapsed month of the current year.
    Keeps the exact service_monthly.json shape but with generic keys
    'cur_fields'/'cur_rows' for the current (partial) month (was 'july_fields'/
    'july_rows' when this was hardcoded to July) — service_live.py updated to match.
    """
    print("== Service monthly (FC v1 per month) ==")
    import calendar
    monthly = []
    cur_fields = cur_rows = None
    for m in range(1, today.month + 1):
        m_from = dt.date(today.year, m, 1)
        last_day = calendar.monthrange(today.year, m)[1]
        m_to = dt.date(today.year, m, last_day)
        is_current = (m == today.month)
        if is_current:
            m_to = today
        params = [{"name": "From", "value": m_from.isoformat()}, {"name": "To", "value": m_to.isoformat()},
                  {"name": "IncludeInactive", "value": False}]
        fields, rows = fetch_report("technician", 328361546, params, f"FC v1 {MONTHS[m-1]}")
        c = {n: i for i, n in enumerate(fields)}
        jobs = opp = converted = 0
        rev = 0.0
        for row in rows:
            if str(row[c["TechnicianBusinessUnit"]]).strip() != "HVAC - Service":
                continue
            cj = float(row[c["CompletedJobs"]] or 0)
            if cj <= 0:
                continue
            o = float(row[c["Opportunity"]] or 0)
            cv = float(row[c["OpportunityConversionRate"]] or 0)
            jobs += cj
            opp += o
            converted += round(o * cv)
            rev += float(row[c["CompletedRevenueWithAdjustments"]] or 0)
        monthly.append({"month": MONTHS[m - 1], "rev": round(rev), "jobs": int(jobs), "opp": int(opp),
                         "converted": int(converted),
                         "conv": round(converted / opp * 1000) / 10 if opp else 0})
        if is_current:
            cur_fields, cur_rows = fields, rows
        time.sleep(1)
    path = os.path.join(HERE, "cache", "service_monthly.json")
    if not cur_rows:
        # Legitimate on the 1st of a month before any job completes, so this is a
        # loud note rather than a refusal — but it means the whole MTD half of the
        # Service deck will render zeros, which must not pass unremarked.
        print(f"  WARN: FC v1 returned 0 rows for the CURRENT month ({MONTHS[today.month-1]}) — "
              "every MTD figure on the Service deck will be 0/$0 this build.")
    if not monthly:
        raise RuntimeError("service_monthly: no months built at all — refusing to write an "
                           "empty monthly series over the existing cache.")
    payload = {"monthly": monthly, "cur_fields": cur_fields, "cur_rows": cur_rows}
    with open(path + ".tmp", "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    print(f"  service_monthly -> {len(monthly)} months, current month rows: {len(cur_rows or [])}")


def main():
    today, ytd_from, mtd_from = _date_range()
    print(f"Refreshing staging caches — YTD {ytd_from} -> {today}, MTD {mtd_from} -> {today}")
    fetch_ca(today, ytd_from, mtd_from)
    fetch_service(today, ytd_from)
    fetch_service_monthly(today)
    print("Done.")


if __name__ == "__main__":
    main()
