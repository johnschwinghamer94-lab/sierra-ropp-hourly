"""DAILY LTO bonus-sheet backfill — recurring.

The Live Feed only refreshes rows it still actively tracks (today's board), so a TGL
created days ago that RUNS later never gets its ran/sold updated and sits SCHEDULED/empty
on the sheet. This recomputes ran/sold/sameDay/canceled for every one of John's TGLs over
a trailing window and fires op:"update" — the webhook's setIfSafe fills empty cells and
never overwrites John's manual entries; the sheet dedupes by source call.

Default: trailing 15 days ending today, live. Override: `python bonus_backfill.py START END [--dry]`.
Needs env SHEET_WEBHOOK, and ServiceTitan creds (ST_CREDS_JSON is materialized here for cloud).
"""
import json, os, sys, urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# --- creds (cloud): materialize ~/.servicetitan/sierra.json from the secret, like cloud_fetch.py
_fp = Path.home() / ".servicetitan" / "sierra.json"
if not _fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
    _fp.parent.mkdir(parents=True, exist_ok=True)
    _fp.write_text(os.environ["ST_CREDS_JSON"])

sys.path.insert(0, str(Path(__file__).parent))
import st_client as st  # noqa: E402
from livefeed_sync import (paged, parse_utc, fmt_t, chunked_get, lead_ca,  # noqa: E402
                           lead_sameday, SHEET_EXCLUDE)

WEBHOOK = os.environ.get("SHEET_WEBHOOK", "").strip()
DRY = "--dry" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) >= 2:
    start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
else:
    end = date.today(); start = end - timedelta(days=14)   # trailing 15 days inclusive

# TGL count-it rule (self-contained; mirrors sierra-ops _sierra_paths.is_tgl_type)
TGL_TYPE_EXCLUDES = ("iaq", "thermostat", "humidifier", "air scrubber",
                     "duct clean", "plumb", "water heater", "water treatment", "costco")
def is_tgl_type(name):
    if not (name or "").startswith("Estimate"):
        return False
    low = name.lower()
    return True if "tgl" in low else not any(x in low for x in TGL_TYPE_EXCLUDES)

iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
def utc0(d): return datetime.combine(d, datetime.min.time()).astimezone().astimezone(timezone.utc)

if not WEBHOOK and not DRY:
    sys.exit("FAIL: SHEET_WEBHOOK not set")

jts = {t["id"]: t.get("name", "") for t in paged("/jpm/v2/tenant/{tenant}/job-types", {})}
emps = {t["id"]: t.get("name", "") for t in paged("/settings/v2/tenant/{tenant}/technicians", {})}

# 1. John's TGLs over the window
tgls = []
d = start
while d <= end:
    for lj in paged("/jpm/v2/tenant/{tenant}/jobs",
                    {"createdOnOrAfter": iso(utc0(d)), "createdBefore": iso(utc0(d + timedelta(days=1)))}):
        gls = lj.get("jobGeneratedLeadSource") or {}
        src = gls.get("jobId")
        if not src or not is_tgl_type(jts.get(lj.get("jobTypeId")) or ""):
            continue
        if emps.get(gls.get("employeeId")) in SHEET_EXCLUDE:
            continue
        tgls.append({"src": str(src), "lead_id": lj["id"],
                     "tech": emps.get(gls.get("employeeId")) or "?",
                     "date": d.isoformat(), "t": fmt_t(parse_utc(lj.get("createdOn")))})
    d += timedelta(days=1)

# 2. lead-job status + sold estimates
lead_jobs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", [t["lead_id"] for t in tgls])}
sold_ids = set()
for e in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": iso(utc0(start))}):
    if e.get("jobId") in lead_jobs and (e.get("soldOn") or ((e.get("status") or {}).get("name") == "Sold")):
        sold_ids.add(e["jobId"])

# 3. dedupe by source call — canceled duplicates drop when a real lead exists on the same call
def status_of(lid): return (lead_jobs.get(lid) or {}).get("jobStatus")
bysrc = {}
for t in tgls:
    bysrc.setdefault(t["src"], []).append(t)
chosen = []
for src, group in bysrc.items():
    pool = [g for g in group if status_of(g["lead_id"]) != "Canceled"] or group
    pool.sort(key=lambda g: (g["lead_id"] not in sold_ids, status_of(g["lead_id"]) != "Completed", g["date"], g["t"]))
    chosen.append(pool[0])

def post(row):
    req = urllib.request.Request(WEBHOOK, data=json.dumps(row).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "sierra-ops"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()

posted = 0
for t in sorted(chosen, key=lambda x: (x["date"], x["t"])):
    lj = lead_jobs.get(t["lead_id"])
    if not lj:
        continue
    stj = lj.get("jobStatus")
    canceled = stj == "Canceled"
    ca = lead_ca(t["lead_id"])
    ran = "RYAN EMAIL" if (ca and ca.split()[0].upper() == "RYAN") else ("Y" if stj == "Completed" else "")
    sold = "Y" if t["lead_id"] in sold_ids else ("N" if stj == "Completed" else "")
    sd = lead_sameday(t["lead_id"], t["date"])
    if not (ran or sold or canceled):        # still scheduled, not run yet — leave blank
        continue
    if DRY:
        print(f"{t['date']} {t['tech'][:16]:16} {t['src']:>10}  ran={ran or '·':10} sold={sold or '·'} "
              f"{'CANCELED' if canceled else ('SAME DAY' if sd else 'SCHEDULED')}")
    else:
        post({"op": "update", "jobNumber": t["src"], "ran": ran, "sold": sold,
              "sameDay": sd, "canceled": canceled})
    posted += 1

print(f"bonus_backfill {start}..{end}: {len(tgls)} leads -> {len(chosen)} calls; "
      f"{'DRY, would fill' if DRY else 'filled'} {posted}.")
