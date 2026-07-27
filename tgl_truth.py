#!/usr/bin/env python3
"""ServiceTitan ground-truth for TGL (turned-generated-lead) creation, used by
the SILO coaching pipeline to reconcile call outcomes against real ticket data.

A TGL = an Estimate-type lead job whose jobGeneratedLeadSource.jobId points at
the source call job (the job the tech was physically running when the flip
happened). Job-type NAME must start with "Estimate" — Install-type lead jobs
do NOT count as TGLs (that's an install booked off an already-flipped lead,
not the flip itself). Same definition livefeed_sync.py uses for the live
board's TGL counter — see its fetch_today()/dept_leads logic.

Writes tgl_truth/<PT date of LEAD creation>.json for each of the trailing 8 PT
dates (today back 7 days), every run, even when a day is empty — downstream
consumers (coaching prompts) always find all 8 files. Keyed by the SOURCE
call job's NUMBER (transcripts identify calls by job number, not job id):

    {"generated": "<iso>", "tgls": {"<source job number>": {"lead": "<lead job number>", "createdOn": "<iso>"}}}

Idempotent — safe to run every 15 min from the workflow. Creds come from
~/.servicetitan/sierra.json, materialized from ST_CREDS_JSON if absent (same
idiom as cloud_fetch.py).
"""
import json, os, pathlib, sys, time
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ── creds (cloud_fetch.py idiom: materialize ~/.servicetitan/sierra.json from
#    ST_CREDS_JSON if it isn't already on disk) ─────────────────────────────
_CREDS_PATH = pathlib.Path.home() / ".servicetitan" / "sierra.json"
if not _CREDS_PATH.exists():
    raw = os.environ.get("ST_CREDS_JSON", "").strip()
    if raw:
        _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CREDS_PATH.write_text(raw)
        os.chmod(_CREDS_PATH, 0o600)

import st_client as st

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:
    PT = None  # fall back to naive local time is not acceptable here — fail loud instead

OUT_DIR = HERE / "tgl_truth"


def now_pt():
    return datetime.now(PT) if PT else datetime.now(timezone.utc)


def parse_utc(s):
    if not s:
        return None
    try:
        base = s.replace("Z", "").split(".")[0].split("+")[0]
        dt = datetime.fromisoformat(base).replace(tzinfo=timezone.utc)
        return dt.astimezone(PT) if PT else dt
    except Exception:
        return None


def paged(path, params, tenant="SIE"):
    out, page = [], 1
    while True:
        r = st.api_get(path, dict(params, page=page, pageSize=200), tenant)
        out += r.get("data", [])
        if not r.get("hasMore"):
            return out
        page += 1
        time.sleep(0.15)


def chunked_get(path, ids, extra=None):
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 50):
        params = dict(extra or {})
        params["ids"] = ",".join(str(x) for x in ids[i:i + 50])
        out += paged(path, params)
        time.sleep(0.15)
    return out


_JT_CACHE = {}
def job_type_names():
    """Job-type id -> name, cached for the life of this process (mirrors the
    per-day cache in livefeed_sync._lookups — job types don't churn intraday)."""
    if not _JT_CACHE:
        for t in paged("/jpm/v2/tenant/{tenant}/job-types", {}):
            _JT_CACHE[t["id"]] = t.get("name", "")
    return _JT_CACHE


def main():
    today_pt = now_pt().date()
    trailing_dates = [today_pt - timedelta(days=i) for i in range(7, -1, -1)]  # oldest -> newest, 8 dates
    window_start_pt = trailing_dates[0]

    day0_utc = datetime(window_start_pt.year, window_start_pt.month, window_start_pt.day,
                         tzinfo=PT).astimezone(timezone.utc) if PT else \
               datetime(window_start_pt.year, window_start_pt.month, window_start_pt.day, tzinfo=timezone.utc)
    created_after = day0_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    jts = job_type_names()

    buckets = {d.isoformat(): {} for d in trailing_dates}  # PT date -> {src_num: {...}}
    total = 0
    for lj in paged("/jpm/v2/tenant/{tenant}/jobs", {"createdOnOrAfter": created_after}):
        gls = lj.get("jobGeneratedLeadSource") or {}
        src = gls.get("jobId")
        if not src:
            continue
        jtn = jts.get(lj.get("jobTypeId")) or ""
        # Install-type lead jobs booked off an already-flipped lead never count
        # (they're the sale of the TGL, not its creation).
        if jtn.startswith("Install"):
            continue
        if not jtn.startswith("Estimate"):
            continue
        created_iso = lj.get("createdOn") or ""
        created_pt = parse_utc(created_iso)
        if not created_pt:
            continue
        dkey = created_pt.date().isoformat()
        if dkey not in buckets:
            continue  # outside the trailing-8 window (created_after has some slack for paging)
        # need the SOURCE job's number — resolved below in a batch pass
        buckets[dkey][src] = {"lead_num": lj.get("jobNumber", ""), "createdOn": created_iso}
        total += 1

    # batch-resolve source job ids -> job numbers
    src_ids = sorted({sid for b in buckets.values() for sid in b})
    src_numbers = {}
    if src_ids:
        for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", src_ids):
            src_numbers[j["id"]] = j.get("jobNumber", "")

    OUT_DIR.mkdir(exist_ok=True)
    generated = now_pt().isoformat()
    counts = []
    for d in trailing_dates:
        dkey = d.isoformat()
        tgls = {}
        for sid, info in buckets[dkey].items():
            num = str(src_numbers.get(sid, sid))
            tgls[num] = {"lead": str(info["lead_num"]), "createdOn": info["createdOn"]}
        payload = {"generated": generated, "tgls": tgls}
        (OUT_DIR / f"{dkey}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        counts.append((dkey, len(tgls)))

    print("tgl_truth: " + ", ".join(f"{d}={n}" for d, n in counts) + f" | total={total}")


if __name__ == "__main__":
    main()
