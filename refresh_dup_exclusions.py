#!/usr/bin/env python3
"""Recompute cancel_dup_exclusions.json from the live ServiceTitan API.

Rule (matches the Cancellation-tab policy): a canceled Estimate/TGL ticket is a "true
duplicate" that should NOT count as a cancellation IFF
  (1) it was canceled with a duplicate reason ("Duplicate entry" / "Avoca Duplicate"), AND
  (2) an actual duplicate TGL ticket exists at the same location, created within 21 days, AND
  (3) that duplicate partner job was NOT canceled (it completed / is live).
If BOTH tickets are canceled, it still counts (kept).

Writes exclude=[jobNumbers] to cancel_dup_exclusions.json. Fail-safe: on any API failure
it leaves the existing file untouched and exits non-zero, so a bad run never re-inflates
the tab or drops real cancellations.

Cloud: run after cloud_fetch.py has materialized ~/.servicetitan/sierra.json.
"""
import json, os, sys, time, re, datetime as dt, urllib.error
from pathlib import Path
import st_client as st

HERE = Path(__file__).parent
OUT = HERE / "cancel_dup_exclusions.json"
REASON_REPORT = ("operations", 537524549)   # Cancellation Reason Report (has CancelReason)
TWIN_WINDOW = 21
DUP_REASON = ("duplicate", "duo")

def call(fn, *a, **k):
    for _ in range(8):
        try:
            return fn(*a, **k)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "15") or 15) + 1); continue
            raise
    raise RuntimeError("retries exhausted")

def pdate(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(re.sub(r"\.\d+", "", str(s)).replace("Z", "").split("+")[0])

def run():
    today = dt.date.today()
    frm = dt.date(today.year, 1, 1).isoformat()
    to = today.isoformat()

    # 1. Duplicate-reason canceled jobs (this year)
    rows, page, fields = [], 1, None
    while True:
        r = call(st.run_report, REASON_REPORT[0], REASON_REPORT[1],
                 [{"name": "DateType", "value": 2}, {"name": "From", "value": frm}, {"name": "To", "value": to}],
                 page=page, page_size=5000)
        fields = [f["name"] for f in r.get("fields", [])]; rows += r.get("data", [])
        if not r.get("hasMore") or not r.get("data"):
            break
        page += 1; time.sleep(1)
    ci = {n: i for i, n in enumerate(fields)}
    dup_jobs = [str(x[ci["JobNumber"]]).strip() for x in rows
                if any(k in str(x[ci["CancelReason"]]).lower() for k in DUP_REASON)]
    dup_jobs = sorted(set(dup_jobs))
    if not dup_jobs:
        raise RuntimeError("no duplicate-reason cancellations found — refusing to overwrite")

    # 2. Job details for those cancellations
    jobs = {}
    for i in range(0, len(dup_jobs), 50):
        grp = dup_jobs[i:i+50]
        r = call(st.api_get, "/jpm/v2/tenant/{tenant}/jobs", {"ids": ",".join(grp), "pageSize": 200})
        for j in r.get("data", []):
            jobs[str(j["id"])] = j
        time.sleep(0.3)

    # 3. TGL job-type ids
    jt, page = {}, 1
    while True:
        r = call(st.api_get, "/jpm/v2/tenant/{tenant}/job-types", {"page": page, "pageSize": 500})
        for j in r.get("data", []):
            jt[j["id"]] = j.get("name", "")
        if not r.get("hasMore") or not r.get("data"):
            break
        page += 1; time.sleep(0.3)
    TGL = {tid for tid, nm in jt.items() if "TGL" in nm.upper()}

    # 4. Twin scan per location
    exclude, kept = [], []
    loc_cache = {}
    for jn, j in jobs.items():
        loc = j.get("locationId"); cre = pdate(j.get("createdOn"))
        if loc not in loc_cache:
            r = call(st.api_get, "/jpm/v2/tenant/{tenant}/jobs", {"locationId": loc, "pageSize": 200})
            loc_cache[loc] = r.get("data", []); time.sleep(0.3)
        near = []
        for x in loc_cache[loc]:
            if str(x["id"]) == str(jn) or x.get("jobTypeId") not in TGL:
                continue
            xc = pdate(x.get("createdOn"))
            if xc and cre and abs((xc - cre).days) <= TWIN_WINDOW:
                near.append(x.get("jobStatus"))
        if not near:
            continue                                    # no real duplicate TGL ticket -> leave it counting
        if any(s != "Canceled" for s in near):
            exclude.append(jn)                          # partner survived -> exclude
        else:
            kept.append(jn)                             # both canceled -> keep counting

    total = len(exclude) + len(kept)
    note = ("* %d true duplicate TGL tickets identified in cancellations. %d excluded here "
            "(duplicate of a job that was completed). %d retained (both tickets canceled)."
            % (total, len(exclude), len(kept)))
    payload = {"exclude": sorted(exclude), "keptBothCanceled": sorted(kept),
               "trueDupsTotal": total, "excludedCount": len(exclude),
               "asOf": today.isoformat(),
               "rule": ("Exclude a canceled Estimate/TGL ticket only if a duplicate TGL ticket exists "
                        "at the same location within 21 days AND that partner job was not canceled. "
                        "If both are canceled it still counts."),
               "note": note}
    OUT.write_text(json.dumps(payload, indent=1))
    print("OK:", note)

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("refresh_dup_exclusions FAILED (existing list kept):", e, file=sys.stderr)
        sys.exit(1)
