#!/usr/bin/env python3
"""HVAC-Service Live Feed sync — per-job lifecycle tracker for the Service dept
live feed (clone of livefeed_sync.py's SILO engine, retargeted at HVAC Service).

Tracks every job today that has a roster Service tech on it (BU 333 techs,
teams "2a Service NO Sam Maintenance" / "2b Service with SAM Maintenance"),
through:
    DISPATCHED -> ON SITE -> OPTIONS ($) -> SIGNED ($) -> DONE
Service techs run both HVAC - Service (333) and HVAC - Maintenance (342817560)
jobs; the job's own BU is carried for display, not filtered on.

Entity APIs for everything the board is built from:
    settings/v2/technicians (roster), jpm/appointments + jobs,
    dispatch/appointment-assignments, crm/customers, sales/estimates
    (options = active unsold; signed = soldOn today), forms/v2 job attachments
    (photo count probe).
ONE reporting-API call, on a 5-minute cache: report 635369650 (Field Pro
    recordings). ServiceTitan exposes call-recording status nowhere else, and
    the reporting API is rate-limited, so it is fetched once per FP_REFRESH_SECS
    for the whole board and never per job — see fetch_fieldpro_today().

Runs in TWO environments (same file, keep private repo & servicetitan/ in sync):
  Windows (default, no env): publishes servicefeed.json LOCALLY only
    (servicetitan/ folder) — no git, no cloud push. NEVER posts to any
    bonus-sheet webhook (livefeed_sync already logs every dept TGL/lead;
    this file must not duplicate that).
  Cloud (SERVICEFEED_CLOUD=1, GitHub Actions): publishes servicefeed.json +
    servicefeed_state.json to the dashboard repo via the GitHub contents API
    with DASHBOARD_TOKEN (PAT commits trigger the Pages deploy; GITHUB_TOKEN
    commits would NOT). State is seeded from the repo at session start so
    5-hour relay sessions hand off cleanly. Creds come from ST_CREDS_JSON
    (materializes ~/.servicetitan/sierra.json). Ported from livefeed_sync.py's
    proven cloud machinery.

Usage:
    python servicefeed_sync.py            # loop: one cycle / 90s, 05:30-midnight
    python servicefeed_sync.py --once     # single cycle now (ignores time window)
    python servicefeed_sync.py --dry      # single cycle, print JSON, no write
"""
import base64, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import st_client as st

CLOUD = os.environ.get("SERVICEFEED_CLOUD") == "1"
SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "servicefeed.json"
STATE = SCRIPT_DIR / "servicefeed_state.json"
LOG = SCRIPT_DIR / "servicefeed_log.txt"

# cloud publish target + session cap (GitHub Actions jobs die at 6 h)
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
MAX_MIN = int(os.environ.get("SERVICEFEED_MAX_MIN", "310"))

CYCLE_SECS = 60   # near-live board (John 7/30)
DAY_START = (5, 30)   # widened from 6:50 (John 7/31)
DAY_END = (23, 59)

SERVICE_BU_ID = 333
SERVICE_TEAMS = {"2a Service NO Sam Maintenance": "2A",
                  "2b Service with SAM Maintenance": "2B"}

# membership / SAM item pattern, found by sampling real sold estimates
# (sku.name starts with SAM / PLSAM, or displayName mentions Membership /
# Maintenance Agreement / Service Agreement)
def _is_membership_item(sku):
    nm = (sku.get("name") or "").upper()
    disp = (sku.get("displayName") or "").lower()
    if nm.startswith("SAM") or nm.startswith("PLSAM"):
        return True
    return any(x in disp for x in ("membership", "maintenance agreement", "service agreement"))


# ── Siro call-recording status (John, 2026-07-30) — INFORMATIONAL ONLY ──────
# DEMOTED 2026-08-05: Siro is NOT how Sierra's service techs record. They record
# through ServiceTitan's Field Pro feature in the ST Mobile app, so a service
# tech with no Siro recording is the normal case, not a violation. This block
# used to feed the full-screen NOT RECORDING takeover and was therefore publicly
# accusing techs of skipping a tool they never use. The card's `siro` field is
# kept (unchanged shape) as a soft in-flight indicator — see the `rec` field and
# fetch_fieldpro_today() below for the signal the alert actually runs on.
#
# Minimal port of livefeed_sync.py's Siro client (same mint/auth pattern,
# same team id, same UA). Cached module-lifetime token; recordings list is
# cached and refreshed at most every 5 minutes (the relay cycles every ~90s,
# don't hammer Siro every cycle). FAILS OPEN on any error (creds absent,
# network, 4xx): returns None, and the caller sets siro:null on every job /
# siroToday:null on the payload rather than ever crashing the relay.
SIRO_TEAM_ID = "Q42L8L"
SIRO_TOKEN_URL_FMT = "https://functions.siro.ai/api-externalApi/v1/core/oauth/apps/{client_id}/access-token"
SIRO_API_BASE = "https://api.siro.ai/v1/core"
SIRO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_SIRO_LOCAL_API_KEY = Path.home() / ".siro_api_key"
_SIRO_LOCAL_OAUTH_APP = Path.home() / ".siro_oauth_app.json"
_SIRO_LOCAL_TEST_USER_ID = "RKnwDLk8seVkMrLIhDdnSOVsEu73"
_SIRO_TOKEN = {"tok": None}
_SIRO_CACHE = {"ts": 0.0, "data": None}   # data: {norm_full_name: {"n": int, "starts": [datetime]}}
SIRO_REFRESH_SECS = 90   # near-live: REC-now badge tracks in-progress recordings


def _siro_creds():
    api_key = os.environ.get("SIRO_API_KEY")
    client_id = os.environ.get("SIRO_CLIENT_ID")
    client_secret = os.environ.get("SIRO_CLIENT_SECRET")
    user_id = os.environ.get("SIRO_USER_ID")
    if not api_key and _SIRO_LOCAL_API_KEY.exists():
        try:
            api_key = _SIRO_LOCAL_API_KEY.read_text().strip()
        except OSError:
            pass
    if (not client_id or not client_secret) and _SIRO_LOCAL_OAUTH_APP.exists():
        try:
            app = json.loads(_SIRO_LOCAL_OAUTH_APP.read_text())
            client_id = client_id or app.get("clientID")
            client_secret = client_secret or app.get("clientSecret")
        except (OSError, json.JSONDecodeError):
            pass
    if not user_id and _SIRO_LOCAL_API_KEY.exists() and _SIRO_LOCAL_OAUTH_APP.exists():
        user_id = _SIRO_LOCAL_TEST_USER_ID
    return api_key, client_id, client_secret, user_id


def _siro_mint_token(api_key, client_id, client_secret, user_id):
    url = SIRO_TOKEN_URL_FMT.format(client_id=client_id)
    r = requests.post(url, json={"clientSecret": client_secret, "userId": user_id, "scope": "read"},
                       headers={"Authorization": f"Bearer {api_key}", "User-Agent": SIRO_UA}, timeout=30)
    r.raise_for_status()
    return r.json()["accessToken"]


def _siro_fetch_recordings(token):
    """All of today's recordings (newest-first, cursor-paginated). A single
    limit=50 page misses most of the day by noon on a 56-tech team — that
    produced false NOT RECORDING alerts (J Boothe, 7/30)."""
    local_mid = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = local_mid.astimezone(timezone.utc).isoformat()
    results, cursor = [], None
    for _ in range(12):  # safety cap: 600 recordings/day is far beyond reality
        url = f"{SIRO_API_BASE}/recordings?limit=50"  # org-wide: Q42L8L = SILO-only, service techs live outside it
        if cursor:
            url += f"&cursor={cursor}"
        r = requests.get(url, headers={"x-siro-auth-token": token, "User-Agent": SIRO_UA}, timeout=30)
        if r.status_code == 401:
            raise PermissionError("401")
        r.raise_for_status()
        data = r.json()
        recs, cursor = data.get("data", []), data.get("cursor")
        stop = False
        for rec in recs:
            if (rec.get("dateCreated") or "") >= cutoff or (rec.get("result") or "").strip().lower() == "in progress":
                results.append(rec)
            else:
                stop = True
                break
        if stop or not cursor:
            break
    return results


import re as _re
_siro_norm = lambda s: " ".join(_re.sub(r"\(.*?\)|[^a-z ]", " ", (s or "").lower()).split())


def _siro_rec_dt(rec):
    """Best-effort creation/start timestamp for a Siro recording object —
    field name isn't pinned down in our docs, so try the plausible ones."""
    for k in ("dateCreated", "createdAt", "createdOn", "startedAt", "startTime", "created", "start"):
        v = rec.get(k)
        if v:
            dt = parse_utc(v) if isinstance(v, str) and ("T" in v or "Z" in v) else None
            if dt:
                return dt
            try:
                # epoch seconds/millis fallback
                num = float(v)
                if num > 1e12:
                    num /= 1000.0
                return datetime.fromtimestamp(num, tz=timezone.utc).astimezone()
            except Exception:
                continue
    return None


def fetch_siro_today():
    """Today's (local/PT) Siro recordings, grouped by normalized rep full
    name: {"n": count, "starts": [aware local datetimes]}. Cached at most
    SIRO_REFRESH_SECS; returns None (and logs once) on any failure or when
    creds are unavailable — caller must degrade silently."""
    now_ts = time.time()
    if _SIRO_CACHE["data"] is not None and now_ts - _SIRO_CACHE["ts"] < SIRO_REFRESH_SECS:
        return _SIRO_CACHE["data"]
    api_key, client_id, client_secret, user_id = _siro_creds()
    if not all([api_key, client_id, client_secret, user_id]):
        log("siro: creds absent — siro:null this cycle")
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = None
        return None
    try:
        tok = _SIRO_TOKEN["tok"]
        if not tok:
            tok = _siro_mint_token(api_key, client_id, client_secret, user_id)
            _SIRO_TOKEN["tok"] = tok
        try:
            recs = _siro_fetch_recordings(tok)
        except PermissionError:
            tok = _siro_mint_token(api_key, client_id, client_secret, user_id)
            _SIRO_TOKEN["tok"] = tok
            recs = _siro_fetch_recordings(tok)
        today_local = datetime.now().astimezone().date()
        out = {}
        now_utc = datetime.now(timezone.utc)
        for rec in recs:
            live = (rec.get("result") or "").strip().lower() == "in progress"
            dt = _siro_rec_dt(rec)
            # zombie guard: an in-progress recording older than 3h is a stuck
            # upload (11.6h zombie seen 7/31), not an active call
            if live and dt and (now_utc - dt.astimezone(timezone.utc)).total_seconds() > 3*3600:
                live = False
            # in-progress = happening now, keep it even if the timestamp field is absent
            if not live and (not dt or dt.date() != today_local):
                continue
            fn = _siro_norm(rec.get("repFirstName"))
            ln = _siro_norm(rec.get("repLastName"))
            full = _siro_norm(f"{fn} {ln}").strip()
            if not full:
                continue
            ent = out.setdefault(full, {"n": 0, "starts": [], "live": False, "liveStarts": []})
            ent["n"] += 1
            if dt:
                ent["starts"].append(dt)
            if live:
                ent["live"] = True
                if dt:
                    ent["liveStarts"].append(dt)
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = out
        return out
    except Exception as ex:
        log("siro FAILED (fail-open, siro:null): " + repr(ex)[:150])
        _SIRO_CACHE["ts"] = now_ts
        _SIRO_CACHE["data"] = None
        return None


def _siro_match(tech_name, siro_data):
    """Tolerant match of a roster tech name to a Siro rep entry — exact
    normalized full-name match first, else first-name match against any
    Siro entry's first token. None if no Siro data / no match."""
    if siro_data is None:
        return None
    norm = _siro_norm(tech_name)
    if not norm:
        return None
    if norm in siro_data:
        return siro_data[norm]
    # strict fallback: last names must match, first names must be compatible
    # (equal or initial/prefix). A bare first-name fallback mismatched Juan
    # Bernal <- Juan Tlatenchi on 7/30 — never match on first name alone.
    tp = norm.split()
    if len(tp) < 2:
        return None
    tf, tl = tp[0], tp[-1]
    for k, v in siro_data.items():
        kp = k.split()
        if len(kp) < 2:
            continue
        kf, kl = kp[0], kp[-1]
        if kl != tl:
            continue
        if kf == tf or kf.startswith(tf) or tf.startswith(kf):
            return v
    return None


# ── ServiceTitan Field Pro call recording (John, 2026-08-05) ────────────────
# The real source of truth for "did this visit get recorded", and the ONLY
# thing allowed to raise the NOT RECORDING red alert.
#
# Source: Reporting API report 635369650 "Field Pro Recordings Diagnostics",
# category "other", params From / To / TechnicianIds / IncludeAppEvents.
# Columns: Timestamp, JobNumber, Technician, RecordingId, Source, Event, Data.
#
# FIVE MEASURED FACTS (all verified against live data 2026-08-05). Every one of
# them is load-bearing for not false-accusing a tech — re-measure before you
# "simplify" any of them.
#
#  1. Technician is the literal string "Unknown technician" and RecordingId is
#     null on 100% of rows. Both columns are dead weight. JobNumber is populated
#     on every row and is the only usable identity, so the join is PER JOB.
#     Never match on tech name — name matching is exactly what produced the
#     earlier crop of false positives (Juan Bernal <- Juan Tlatenchi, 7/30).
#
#  2. TIMEZONE TRAP: Timestamp is UTC digits wearing a WRONG "-07:00" suffix.
#     Proof: job 669192850's "Recording stopped" row reads
#     2026-08-04T23:53:18-07:00 while the job's own completedOn is
#     2026-08-04T23:53:18.649Z — identical digits. Parse the digits as UTC and
#     convert to local; discard the printed offset. Anchor: job 671253374 reads
#     2026-08-05T16:25:24-07:00 and is truly 9:25:24 AM PDT.
#
#  3. From/To filter on the JOB'S COMPLETION DATE, not on the event timestamp.
#     A job completed 8/4 drags ALL of its recording events along even if they
#     happened in June. The corollary is the dangerous one: A JOB WITH NO
#     completedOn RETURNS NO ROWS AT ALL. Measured 8/5 11:37a — 17 of 17
#     finished service jobs were in the report; 0 of the 25 jobs that had a tech
#     standing on site right then were. "Absent from the report" therefore means
#     "not finished yet" far more often than it means "not recording", and every
#     in-flight job MUST resolve UNKNOWN.
#
#  4. Once a job completes, the report catches up almost immediately. Measured
#     8/5: job 667725614 completed 18:49:14Z and was present in a pull taken
#     88 seconds later. FP_SETTLE_MIN below is ~14x that margin.
#
#  5. Event values are full sentences ("Recording initiated from the
#     ServiceTitan Mobile app"), so match on substring, never equality.
#
#  6. *** THE ATTRIBUTION GAP — why this signal may not accuse anyone yet ***
#     The default (unfiltered) pull returns only job-attributed rows: Event
#     "Recording initiated/stopped from the ServiceTitan Mobile app" with a
#     populated JobNumber. Pass TechnicianIds and the report ALSO returns the
#     mobile app's own telemetry — "Recording started / paused / resumed /
#     interrupted / stopped / cancelled", "consentStatusChange", "User logged
#     in", "Technician Arrived on the Appointment", "Technician Completed the
#     Job" — and on those rows JOBNUMBER IS BLANK for every recording event.
#
#     Measured 8/5 on 8/4 data: Aaron Davis's job 662208158 has NO job-attributed
#     recording row, so a per-job join says "never recorded". His technician-
#     filtered stream shows "Technician Arrived on the Appointment" at 21:57:25
#     and "Recording started" SIXTEEN SECONDS LATER at 21:57:41, under a blank
#     JobNumber. He was recording. Same story for Juan Tlatenchi (jobs 667723254
#     / 668790087 / 671039859). Of the 14 jobs a naive per-job rule would have
#     flagged on 8/4, at least 5 are provably false.
#
#     CONCLUSION: a job's ABSENCE from the job-attributed stream does NOT prove
#     the tech wasn't recording — it may only mean ServiceTitan failed to tie
#     that recording to a job. PRESENCE is solid evidence; ABSENCE is not
#     evidence at all.
#
#  7. *** THE GAP IS NOW CLOSED — fieldpro_attrib.py (2026-08-05) ***
#     The blank-job recording rows DO carry a RecordingId, which groups them
#     into recording SESSIONS, and the same technician-filtered stream carries
#     "Technician Arrived on the Appointment" / "Technician Completed the Job"
#     rows that DO have a job number. fieldpro_attrib.py builds per-tech job
#     windows from those and correlates each orphan session into the window it
#     starts inside, publishing fieldpro_attrib.json — a per-job verdict with
#     the event times and the rule that fired, so a disputed flag can be
#     audited after the fact. This file merges that artifact below; the direct
#     job-attributed signal stays authoritative and the correlator only fills
#     the blank-job gap.
#
#     Replayed against the closed days 8/3 and 8/4: every one of the four
#     known-positive cases (Aaron Davis 662208158, Juan Tlatenchi 667723254 /
#     668790087 / 671039859) resolves as RECORDING via the window rule, and the
#     residual — sessions that could not be placed on any window — is small and
#     is ITSELF handled: a tech with an unplaced real session cannot be accused
#     that day at all (verdict "unknown", reason "uncorrelated-session").
#
# Reports 636320211 / 634963669 describe the same recordings but lag 9-10+
# hours. They must never be used for anything live.
FP_REPORT_CATEGORY = "other"
FP_REPORT_ID = 635369650
FP_REFRESH_SECS = 300     # the relay cycles every 60s; the reporting API is rate
                          # limited (it answers 409 when you crowd it), so one
                          # board-wide pull every 5 min, never one per job
FP_SETTLE_MIN = 20        # a finished job must be this old before "no rows"
                          # is allowed to mean "no recording" (fact 4)

# THE ACCUSATION SWITCH. False = fieldpro_state() never returns "none", so the
# full-screen NOT RECORDING takeover in service.html (which fires only on
# "none") is unreachable.
#
# Even with this True the ONLY thing that can produce "none" is an explicit
# `"v":"none"` verdict from fieldpro_attrib.json, on a fresh artifact, for a
# job that is Done + settled. A missing artifact, a stale one, a job the
# correlator marked "unknown" for ANY reason, or a Field Pro report failure all
# fall through to "unverified", which no alert reads. There is no code path
# from "we don't know" to an accusation.
FP_ACCUSE_OK = True
# The correlator runs every 20 min. An artifact older than this is not trusted
# to ACCUSE (its "recording" verdicts are still honoured — a positive doesn't
# rot, and a job absent from a stale artifact simply resolves unknown).
FP_ATTRIB_MAX_AGE_MIN = 75
FP_ATTRIB_REFRESH_SECS = 300
_FP_CACHE = {"ts": 0.0, "data": None}
_FP_ATTRIB_CACHE = {"ts": 0.0, "data": None}


def _fp_local(ts):
    """Field Pro Timestamp -> aware local datetime. The digits are UTC and the
    '-07:00' suffix in the string is wrong (fact 2) — slice the offset off and
    label the digits UTC. None if unparseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)[:19]).replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return None


def fetch_fieldpro_today():
    """{job_number: {"initiated","stopped","firstInitiated","lastEvent","n"}} for
    today's jobs, or None when the report could not be read.

    None is NOT "nobody recorded" — it is UNKNOWN, and every caller must turn it
    into a state that cannot alert. Publishing a fabricated zero here puts a
    tech's name on a full-screen accusation on the wall, so a failed fetch
    degrades loudly (throttled log) and never quietly.

    Cached FP_REFRESH_SECS; a failure caches None for the same interval so a
    broken report can't be hammered 60x an hour.

    Window is [today, today+1]. From/To key off the job's completion date
    (fact 3) and it is not pinned down whether ServiceTitan evaluates that date
    in UTC or in tenant-local time; a one-day overshoot satisfies both readings.
    The extra jobs are harmless because the join is by job number.
    """
    now_ts = time.time()
    if now_ts - _FP_CACHE["ts"] < FP_REFRESH_SECS:
        return _FP_CACHE["data"]
    today = date.today()
    frm, to = today.isoformat(), (today + timedelta(days=1)).isoformat()
    try:
        rows, fields, page = [], None, 1
        while page <= 10:            # safety cap; a full day is ~600 rows
            # The reporting API answers 409 Conflict when too many report runs
            # are in flight for the tenant, and st_client only retries 429/5xx.
            # graph_hourly / service_data / ropp_live share that budget, so
            # collisions are routine. A 409 that reaches the caller costs the
            # board its recording signal for a full FP_REFRESH_SECS, so absorb
            # a couple here — failing to UNKNOWN is safe but not free.
            for attempt in range(3):
                try:
                    r = st.run_report(FP_REPORT_CATEGORY, FP_REPORT_ID,
                                      [{"name": "From", "value": frm}, {"name": "To", "value": to}],
                                      page=page, page_size=5000)
                    break
                except urllib.error.HTTPError as ex:
                    if ex.code != 409 or attempt == 2:
                        raise
                    time.sleep(5)
            fields = [f["name"] for f in (r.get("fields") or [])] or fields
            rows += r.get("data", [])
            if not r.get("hasMore") or not r.get("data"):
                break
            page += 1
            time.sleep(1)
        # A real Reporting-API response ALWAYS carries a field list, even for a
        # window with zero rows. A missing field list means a malformed/empty
        # 200 body, which would otherwise read as "not one tech recorded today"
        # and light up every card on the board. Zero ROWS is still legal (at
        # 5:30am nothing has completed yet) — zero FIELDS is a fetch failure.
        if not fields:
            raise RuntimeError("no field list in the report response")
        ix = {f: i for i, f in enumerate(fields)}
        for need in ("Timestamp", "JobNumber", "Event"):
            if need not in ix:
                raise RuntimeError("report is missing the %s column" % need)
        out = {}
        for row in rows:
            jn = str(row[ix["JobNumber"]] or "").strip()
            if not jn:
                continue
            ev = str(row[ix["Event"]] or "").lower()
            dtl = _fp_local(row[ix["Timestamp"]])
            ent = out.setdefault(jn, {"initiated": False, "stopped": False,
                                      "firstInitiated": None, "lastEvent": None, "n": 0})
            ent["n"] += 1
            if "initiated" in ev:
                ent["initiated"] = True
                if dtl and (ent["firstInitiated"] is None or dtl < ent["firstInitiated"]):
                    ent["firstInitiated"] = dtl
            elif "stopped" in ev:
                ent["stopped"] = True
            if dtl and (ent["lastEvent"] is None or dtl > ent["lastEvent"]):
                ent["lastEvent"] = dtl
        _FP_CACHE["ts"] = now_ts
        _FP_CACHE["data"] = out
        return out
    except Exception as ex:
        _FP_CACHE["ts"] = now_ts
        _FP_CACHE["data"] = None
        warn_throttled("fieldpro", "Field Pro report %d FAILED — recording status is "
                       "UNKNOWN for every job this cycle, NOT RECORDING cannot alert: %s"
                       % (FP_REPORT_ID, repr(ex)[:180]))
        return None


def fetch_fieldpro_attrib():
    """{job_number: verdict} from fieldpro_attrib.py's side artifact, or None.

    None = the correlator's opinion is unavailable this cycle, which degrades
    every job to the pre-correlator behaviour (positive evidence still counts,
    nothing can be accused). Never a fabricated empty dict: {} would read as
    "the correlator ran and cleared everybody", and an accusation must never
    come from a missing file.

    Cloud reads it from the dashboard repo (where fieldpro_attrib.py publishes
    it); local reads the file next to this script. Day-gated: an artifact for
    another date is discarded outright.
    """
    now_ts = time.time()
    if now_ts - _FP_ATTRIB_CACHE["ts"] < FP_ATTRIB_REFRESH_SECS:
        return _FP_ATTRIB_CACHE["data"]
    _FP_ATTRIB_CACHE["ts"] = now_ts
    try:
        if CLOUD:
            txt = gh_fetch("fieldpro_attrib.json")
        else:
            p = SCRIPT_DIR / "fieldpro_attrib.json"
            txt = p.read_text(encoding="utf-8") if p.exists() else None
        if not txt:
            _FP_ATTRIB_CACHE["data"] = None
            warn_throttled("fpattrib", "Field Pro attribution artifact MISSING — the "
                           "correlator's verdicts are unavailable, NOT RECORDING cannot alert")
            return None
        j = json.loads(txt)
        if j.get("day") != date.today().isoformat():
            _FP_ATTRIB_CACHE["data"] = None
            warn_throttled("fpattrib", "Field Pro attribution artifact is for %s, not today "
                           "— ignored, NOT RECORDING cannot alert" % j.get("day"))
            return None
        age_min = (now_ts * 1000 - (j.get("generatedMs") or 0)) / 60000.0
        j["_ageMin"] = age_min
        if not j.get("ok"):
            log("Field Pro attribution is PARTIAL — streams failed for %s; their jobs stay "
                "unknown" % ", ".join(j.get("failedTechs") or []))
        _FP_ATTRIB_CACHE["data"] = j
        return j
    except Exception as ex:
        _FP_ATTRIB_CACHE["data"] = None
        warn_throttled("fpattrib", "Field Pro attribution artifact FAILED to load — NOT "
                       "RECORDING cannot alert: %s" % repr(ex)[:180])
        return None


def fieldpro_state(fp, attrib, job_number, job_id, status, completed_local, now):
    """(state, entry) for one card. state is one of:

      "recording"  — ServiceTitan logged Field Pro activity on this job number,
                     or fieldpro_attrib.py correlated a blank-job recording
                     session into this job's on-site window. Positive evidence.
      "unverified" — the job is finished and settled, and nothing — neither the
                     direct job-attributed rows nor the correlator — could
                     establish a recording either way. NOT an accusation.
      "none"       — an asserted violation: the correlator saw this tech arrive
                     and complete this job and saw ZERO recording activity
                     anywhere in that window, on a fresh artifact. This is the
                     only state the red alert reacts to.
      "unknown"    — everything else: report unavailable, job still running,
                     "Done" with no completedOn, or finished too recently for
                     the report to have caught up.

    Precedence is deliberate. The DIRECT signal wins whenever it exists — it is
    ServiceTitan's own job attribution and needs no inference. The correlator is
    consulted only where direct evidence is absent, which is exactly the gap it
    was built for. Every ambiguity lands on a non-accusing state.

    Note that ANY Field Pro row counts as "recording", not just "Recording
    initiated": 2 of the 369 jobs sampled across 8/4-8/5 carried a "Recording
    stopped" with no matching initiate. Ambiguous evidence must never become a
    public accusation.
    """
    if fp is None:
        return "unknown", None
    ent = fp.get(str(job_number or "")) or fp.get(str(job_id or ""))
    if ent:
        return "recording", ent
    if status != "Done" or not completed_local:
        # Still running, or marked Done with no completedOn. Either way the
        # report keys off completion (fact 3) and physically cannot list it.
        return "unknown", None
    if (now - completed_local).total_seconds() < FP_SETTLE_MIN * 60:
        return "unknown", None
    # No direct row. Ask the correlator (fact 7).
    a = ((attrib or {}).get("jobs") or {}).get(str(job_number or ""))
    if a and a.get("v") == "recording":
        # A blank-job recording session landed inside this job's on-site
        # window. Carry the rule + times so the card can show WHY.
        ev = (a.get("ev") or [{}])[0]
        return "recording", {"initiated": True, "stopped": None, "n": len(a.get("ev") or []),
                             "firstInitiated": None, "lastEvent": None,
                             "via": a.get("rule"), "t0": ev.get("t0"), "t1": ev.get("t1")}
    if (FP_ACCUSE_OK and a and a.get("v") == "none"
            and (attrib or {}).get("_ageMin", 1e9) <= FP_ATTRIB_MAX_AGE_MIN):
        return "none", {"initiated": False, "stopped": False, "n": 0,
                        "firstInitiated": None, "lastEvent": None,
                        "via": a.get("rule"), "arr": a.get("arr"), "done": a.get("end")}
    # a is None / "unknown" / a stale artifact -> we do not know. Say so.
    return "unverified", None


def log(msg):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line)
    try:
        if LOG.exists() and LOG.stat().st_size > 400_000:
            LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-1500:]) + "\n", encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


_WARN_ONCE = {}
def warn_throttled(key, msg, every=300):
    """log(msg) at most once per `every` seconds per key — see livefeed_sync."""
    now = time.time()
    if now - _WARN_ONCE.get(key, 0) >= every:
        _WARN_ONCE[key] = now
        log(msg)


# Publish results that mean "the artifact did NOT land". A cycle returning one
# of these is a FAILURE even though nothing raised — see cloud_main(). Kept in
# lockstep with livefeed_sync.py so the two relays count errors identically.
PUBLISH_FAILURE_RESULTS = {"PUSH FAILED"}


def fmt_t(dt_local):
    s = dt_local.strftime("%I:%M%p").lstrip("0")
    return s[:-2] + ("p" if s.endswith("PM") else "a")


def parse_utc(s):
    if not s:
        return None
    try:
        base = s.replace("Z", "").split(".")[0].split("+")[0]
        return datetime.fromisoformat(base).replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return None


def paged(path, params, tenant="SIE"):
    out, page = [], 1
    while True:
        r = st.api_get(path, dict(params, page=page, pageSize=500), tenant)
        # A real ServiceTitan list response always carries page/pageSize/hasMore
        # even when data is empty. A completely empty dict means st_client got a
        # 2xx with an empty body — treat that as a fetch FAILURE, not as "no
        # jobs today", so it counts toward the consecutive-error guard instead
        # of publishing an empty board. (data:[] on a real response is still a
        # legitimate empty result and passes through.)
        if not r:
            raise RuntimeError("empty response body from " + path + " (page %d)" % page)
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


# ── roster (dynamic, cached once/day) ───────────────────────────────────────

def fetch_roster():
    """{lower(tech_name): '2A'|'2B'} for active BU-333 techs on the two target
    teams. Keyed lower-case so lookups are case-insensitive (PRUET/Pruet
    landmine) — use roster_team() to read it, never roster.get() directly."""
    techs = paged("/settings/v2/tenant/{tenant}/technicians", {})
    roster = {}
    for t in techs:
        if not t.get("active"):
            continue
        if t.get("businessUnitId") != SERVICE_BU_ID:
            continue
        team = SERVICE_TEAMS.get(t.get("team"))
        if team:
            nm = (t.get("name") or "").strip()
            if nm:
                roster[nm.lower()] = team
    return roster


def roster_team(roster, name):
    """Case-insensitive team lookup; '' (unassigned) when the tech isn't on
    the 2A/2B roster snapshot — never used to gate inclusion, only labeling."""
    return roster.get((name or "").strip().lower(), "")


def get_roster(state):
    today = date.today().isoformat()
    cached = state.get("roster")
    if cached and cached.get("date") == today and cached.get("map"):
        return cached["map"]
    try:
        rmap = fetch_roster()
    except Exception as ex:
        log("roster fetch FAILED, using stale cache if any: " + repr(ex)[:150])
        return (cached or {}).get("map", {})
    state["roster"] = {"date": today, "map": rmap}
    return rmap


# ── lookups (BU / job-type names), cached once/day ─────────────────────────

_LOOKUPS = {}
def _lookups(today):
    if _LOOKUPS.get("date") != today.isoformat():
        _LOOKUPS["bus"] = {b["id"]: b.get("name", "") for b in paged("/settings/v2/tenant/{tenant}/business-units", {})}
        _LOOKUPS["jts"] = {t["id"]: t.get("name", "") for t in paged("/jpm/v2/tenant/{tenant}/job-types", {})}
        _LOOKUPS["date"] = today.isoformat()
    return _LOOKUPS["bus"], _LOOKUPS["jts"]


# ── photo count probe ───────────────────────────────────────────────────────

_PHOTO_ENDPOINT_OK = None  # None = untested, True once known good this session
def photo_count(job_id, tenant="SIE"):
    """Attachment count for a job, or None when it can't be determined (the UI
    renders nothing rather than a confident 0).

    A failure used to latch _PHOTO_ENDPOINT_OK=False for the WHOLE 5-hour
    session with no log line, so one transient timeout on one job silently
    blanked the photo column for every job until the session rolled. Now: a
    4xx that means "this tenant/endpoint doesn't serve attachments" still
    latches off (once, loudly); anything else is treated as a per-job blip."""
    global _PHOTO_ENDPOINT_OK
    if _PHOTO_ENDPOINT_OK is False:
        return None
    try:
        r = st.api_get("/forms/v2/tenant/{tenant}/jobs/" + str(job_id) + "/attachments",
                       {"page": 1, "pageSize": 1}, tenant)
        _PHOTO_ENDPOINT_OK = True
        tc = r.get("totalCount")
        if tc is not None:
            return tc
        # totalCount not populated by this API — page through cheaply
        n, page = 0, 1
        while True:
            rr = st.api_get("/forms/v2/tenant/{tenant}/jobs/" + str(job_id) + "/attachments",
                            {"page": page, "pageSize": 50}, tenant)
            n += len(rr.get("data", []))
            if not rr.get("hasMore"):
                break
            page += 1
        return n
    except urllib.error.HTTPError as ex:
        if ex.code in (401, 403, 404, 501):
            _PHOTO_ENDPOINT_OK = False
            log("photo probe DISABLED for this session — attachments endpoint returned "
                "HTTP %d on job %s; every card's photo count will be null" % (ex.code, job_id))
        else:
            warn_throttled("photos", "WARN photo probe failed on job %s (HTTP %d) — "
                           "photos:null for that card only" % (job_id, ex.code))
        return None
    except Exception as ex:
        # transient (timeout / network): do NOT blind the whole session for it
        warn_throttled("photos", "WARN photo probe failed on job %s — photos:null for "
                       "that card only: %s" % (job_id, repr(ex)[:120]))
        return None


# ── one polling cycle: fetch ────────────────────────────────────────────────

def fetch_today(roster):
    today = date.today()
    day0 = datetime.combine(today, datetime.min.time()).astimezone().astimezone(timezone.utc)
    day1 = day0 + timedelta(days=1)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")

    appts = paged("/jpm/v2/tenant/{tenant}/appointments",
                  {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
    appts = [a for a in appts if a.get("status") not in ("Canceled",)]
    if not appts:
        return today, [], {}, {}, {}, {}, []

    # appointment-assignments date filters don't work — must go via
    # appointment IDs (same limitation livefeed_sync works around)
    asg = []
    aids = [a["id"] for a in appts]
    for i in range(0, len(aids), 50):
        asg += paged("/dispatch/v2/tenant/{tenant}/appointment-assignments",
                     {"appointmentIds": ",".join(str(x) for x in aids[i:i + 50])})
        time.sleep(0.15)
    # tech_by_appt tracks EVERY assigned tech (not gated on the 2A/2B roster
    # snapshot) — the roster gate here was dropping real signed-today revenue
    # from techs doing HVAC-Service work who aren't in that narrow snapshot
    # (e.g. Bradley Espinoza $9,798, 8/3). Job inclusion below is decided by
    # the job's own business unit, not by who's on the roster; roster is only
    # consulted for the cosmetic 2A/2B team label on a card.
    tech_by_appt = {}
    for a in asg:
        if a.get("active") and a.get("technicianName"):
            tech_by_appt.setdefault(a["appointmentId"], []).append(a["technicianName"])

    # fetch jobs for ALL of today's appointments (bounded to today, cheap) so
    # we can decide inclusion by business unit rather than by tech roster.
    all_job_ids = sorted({a["jobId"] for a in appts})
    if not all_job_ids:
        return today, [], {}, {}, {}, {}, []
    jobs_all = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", all_job_ids)}

    # HVAC-Service job (BU 333) → always in; roster-tech job on any other BU
    # (e.g. HVAC-Maintenance 342817560) → in, per doc note above; else out.
    def _is_tracked(a):
        j = jobs_all.get(a["jobId"])
        if j and j.get("businessUnitId") == SERVICE_BU_ID:
            return True
        return any(roster_team(roster, t) for t in tech_by_appt.get(a["id"], []))

    svc_appts = [a for a in appts if _is_tracked(a)]
    job_ids = sorted({a["jobId"] for a in svc_appts})
    if not job_ids:
        return today, [], {}, {}, {}, {}, []

    jobs = {jid: jobs_all[jid] for jid in job_ids if jid in jobs_all}
    cust_ids = sorted({j.get("customerId") for j in jobs.values() if j.get("customerId")})
    custs = {c["id"]: c.get("name", "") for c in chunked_get("/crm/v2/tenant/{tenant}/customers", cust_ids)}

    bus, jts = _lookups(today)
    for j in jobs.values():
        j["_bu"] = bus.get(j.get("businessUnitId"), "")
        j["_jt"] = jts.get(j.get("jobTypeId"), "")

    # estimates touched today (options) + sold today (money on the job)
    ests = paged("/sales/v2/tenant/{tenant}/estimates", {"modifiedOnOrAfter": iso(day0)})
    time.sleep(0.15)
    ests += [e for e in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": iso(day0)})
             if e["id"] not in {x["id"] for x in ests}]
    est_by_job = {}
    for e in ests:
        if e.get("jobId") in jobs:
            est_by_job.setdefault(e["jobId"], []).append(e)

    # LEADS SET — service equivalent of TGL created: an Estimate-type lead job
    # created today whose jobGeneratedLeadSource points at a tracked job, and
    # whose job-type name startswith "Estimate" (not Install-type). Identical
    # detection logic to livefeed_sync's TGL rule; NO sheet/webhook posting —
    # livefeed_sync already logs every dept lead, this must not duplicate it.
    lead_by_src = {}
    for lj in paged("/jpm/v2/tenant/{tenant}/jobs", {"createdOnOrAfter": iso(day0)}):
        gls = lj.get("jobGeneratedLeadSource") or {}
        src = gls.get("jobId")
        if not src or src not in jobs:
            continue
        jtn = jts.get(lj.get("jobTypeId")) or ""
        if not jtn.startswith("Estimate"):
            continue
        dtl = parse_utc(lj.get("createdOn"))
        ent = lead_by_src.setdefault(src, {"n": 0, "t": None})
        ent["n"] += 1
        if ent["t"] is None and dtl:
            ent["t"] = fmt_t(dtl)

    return today, svc_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src


# ── build payload ────────────────────────────────────────────────────────────

def build(state):
    roster = get_roster(state)
    today, svc_appts, tech_by_appt, jobs, custs, est_by_job, lead_by_src = fetch_today(roster)
    now = datetime.now().astimezone()
    now_s = fmt_t(now)
    dkey = today.isoformat()

    first_run_of_day = state.get("date") != dkey
    if first_run_of_day:
        keep_roster = state.get("roster")
        state.clear()
        state.update({"date": dkey, "jobs": {}, "feed": [], "hourly": {}})
        if keep_roster:
            state["roster"] = keep_roster
    seen = state["jobs"]
    feed = state["feed"]

    def event(icon, text, color):
        if not first_run_of_day:
            feed.insert(0, {"i": icon, "x": text, "t": now_s, "c": color})

    by_job = {}
    for a in sorted(svc_appts, key=lambda x: x.get("start") or ""):
        by_job.setdefault(a["jobId"], a)

    RANK = {"Working": 0, "Dispatched": 1, "Hold": 2, "Scheduled": 3, "Done": 4}
    siro_data = fetch_siro_today()   # one Siro check per cycle (fail-open -> None)
    fp_data = fetch_fieldpro_today() # ST Field Pro recordings (None = UNKNOWN, never 0)
    fp_attrib = fetch_fieldpro_attrib()  # fieldpro_attrib.py's correlated verdicts (None = UNKNOWN)
    siro_techs_seen = set()
    cards = []
    on_site = 0
    opt_total = opt_count = 0
    opt_jobs = set()
    signed_total = 0.0
    signed_jobs = set()
    leads_set_n = 0
    memberships_sold = 0

    for jid, appt in by_job.items():
        j = jobs.get(jid, {})
        techs = sorted(set(sum((tech_by_appt.get(a["id"], []) for a in svc_appts if a["jobId"] == jid), [])))
        team_set = sorted({roster_team(roster, t) for t in techs} - {""})
        cust = custs.get(j.get("customerId"), "") or "—"
        status = appt.get("status") or "Scheduled"
        if j.get("jobStatus") == "Completed" or status == "Done":
            status = "Done"
        js = seen.setdefault(str(jid), {"st": {}, "opt": 0.0, "sold": 0.0, "status": ""})

        for stage, hit in (("dispatched", status in ("Dispatched", "Working", "Done")),
                           ("onsite", status in ("Working", "Done")),
                           ("done", status == "Done")):
            if hit and stage not in js["st"]:
                js["st"][stage] = "✓" if first_run_of_day else now_s
                if stage == "dispatched":
                    event("\U0001F69A", "Dispatched: " + ", ".join(techs) + " @ " + cust, "#9fb3cc")
                elif stage == "onsite":
                    event("\U0001F4CD", "On site: " + ", ".join(techs) + " @ " + cust, "#c084fc")
                elif stage == "done":
                    event("\U0001F3C1", "Completed: " + ", ".join(techs) + " @ " + cust, "#7fb3e8")

        opts_t = opts_n = 0
        opt_time = sold_time = None
        sold_t = 0.0
        sold_flag = False
        membership_sold_job = False
        membership_offered_job = False
        for est in est_by_job.get(jid, []):
            sub = float(est.get("subtotal") or 0)
            st_name = ((est.get("status") or {}).get("name") or "").lower()
            sold = bool(est.get("soldOn")) or st_name == "sold"
            items = est.get("items") or []
            skus = [(i.get("sku") or {}) for i in items]
            is_ca_placeholder = {(s.get("name") or "") for s in skus} == {"CA01"}
            has_membership = any(_is_membership_item(s) for s in skus)
            if has_membership:
                membership_offered_job = True
                if sold:
                    membership_sold_job = True
            if sold:
                sold_t += sub
                sold_flag = True
                dtl = parse_utc(est.get("soldOn"))
                if dtl and dtl.date() == today:
                    sold_time = fmt_t(dtl)
            elif est.get("active") and st_name != "dismissed" and sub > 0:
                opts_t += sub
            if est.get("active") and st_name != "dismissed" and not is_ca_placeholder:
                opts_n += 1
                dtl = parse_utc(est.get("createdOn"))
                if dtl:
                    opt_time = fmt_t(dtl)

        lead = lead_by_src.get(jid)
        lead_n = lead["n"] if lead else 0
        lead_time = lead["t"] if lead else None

        if opts_t > js.get("opt", 0) + 0.5:
            event("\U0001F6E0", "Building options: " + ", ".join(techs) + " @ " + cust +
                  " [$" + format(int(opts_t), ",") + "]", "#F5B324")
        if sold_t > js.get("sold", 0) + 0.5:
            event("\U0001F4B5", "Sold: " + ", ".join(techs) + " @ " + cust +
                  " [+$" + format(int(sold_t - js.get("sold", 0)), ",") + "]", "#7fb3e8")
        prev_lead = js.get("leadN")
        if prev_lead is None:
            js["leadN"] = lead_n
        elif lead_n > prev_lead:
            event("✅", "LEAD SET: " + ", ".join(techs) + " @ " + cust, "#4ADE80")
        js["opt"], js["sold"], js["leadN"], js["status"] = opts_t, sold_t, lead_n, status

        if status == "Working":
            on_site += 1
        if opts_n:
            opt_total += opts_t; opt_count += opts_n; opt_jobs.add(jid)
        if sold_flag:
            signed_jobs.add(jid)
        signed_total += sold_t
        if lead_n:
            leads_set_n += lead_n
        if membership_sold_job:
            memberships_sold += 1

        start_l = parse_utc(appt.get("start"))
        done_l = parse_utc(j.get("completedOn"))

        photos = photo_count(jid)

        # Siro recording status (John, 2026-07-30): "n" = tech's recordings
        # today, "rec" = any recording started within the job's onsite window
        # (appt start - 15min .. now/done). null when Siro data unavailable.
        siro = None
        if techs:
            siro_techs_seen.update(techs)
        if siro_data is not None and techs:
            n_total = 0
            rec_flag = False
            live_now = False
            live_tech = False   # any non-zombie live recording, even from a prior stop
            matched_any = False
            win_start = (start_l - timedelta(minutes=45)) if start_l else None
            win_end = done_l if (status == "Done" and done_l) else now
            for t in techs:
                ent = _siro_match(t, siro_data)
                if not ent:
                    continue
                matched_any = True
                n_total += ent["n"]
                if ent.get("live"):
                    live_tech = True
                if win_start:
                    for st_dt in ent["starts"]:
                        if win_start <= st_dt <= win_end:
                            rec_flag = True
                    # recNow only when a live recording STARTED during THIS visit —
                    # a tech-level live flag lit jobs the recording didn't belong to
                    # (Fabian 7/31: recorder left running from a prior stop)
                    for ldt in ent.get("liveStarts", []):
                        if win_start <= ldt <= win_end:
                            live_now = True
            if live_now:
                rec_flag = True   # recording this visit right now = visit recorded
            if matched_any:
                siro = {"n": n_total, "rec": rec_flag, "recNow": live_now, "liveTech": live_tech}
            else:
                siro = {"n": 0, "rec": False, "recNow": False, "liveTech": False}

        # ServiceTitan Field Pro recording truth for this card. `rec.state` is
        # the ONLY thing the NOT RECORDING alert is allowed to read; it is
        # "unknown" for anything in flight or unverifiable (see fieldpro_state).
        rec_state, rec_ent = fieldpro_state(fp_data, fp_attrib, j.get("jobNumber"), jid,
                                            status, done_l, now)
        rec = {
            "src": "st",
            "state": rec_state,
            "initiated": bool(rec_ent["initiated"]) if rec_ent else None,
            "stopped": bool(rec_ent["stopped"]) if rec_ent else None,
            "first": fmt_t(rec_ent["firstInitiated"]) if (rec_ent and rec_ent["firstInitiated"]) else None,
            "last": fmt_t(rec_ent["lastEvent"]) if (rec_ent and rec_ent["lastEvent"]) else None,
            "n": rec_ent["n"] if rec_ent else 0,
            # "via" is null for the direct job-attributed signal and names the
            # correlation rule when the verdict came from fieldpro_attrib.py —
            # this is the audit trail for a disputed flag.
            "via": (rec_ent or {}).get("via"),
        }

        cards.append({
            "jobId": jid, "jobNumber": j.get("jobNumber", ""),
            "tech": ", ".join(techs), "team": ", ".join(team_set),
            "customer": cust, "bu": j.get("_bu", ""), "jobType": j.get("_jt", ""),
            "status": status, "rank": RANK.get(status, 3),
            "start": fmt_t(start_l) if start_l else "",
            "startIso": appt.get("start") or "9999",
            "stages": {
                "dispatched": js["st"].get("dispatched"),
                "onsite": js["st"].get("onsite"),
                "options": {"t": opt_time, "n": opts_n, "total": int(opts_t)} if opts_n else None,
                "signed": {"t": sold_time, "total": int(sold_t)} if sold_flag else None,
                "leadSet": {"t": lead_time, "n": lead_n} if lead_n else None,
                "done": (fmt_t(done_l) if done_l else None) or js["st"].get("done"),
            },
            "membershipOffered": membership_offered_job,
            "membershipSold": membership_sold_job,
            "photos": photos,
            "siro": siro,
            "rec": rec,
        })

    cards.sort(key=lambda c: c["startIso"])

    if first_run_of_day:
        feed.insert(0, {"i": "\U0001F4E1", "x": "Service Feed online — tracking " + str(len(cards)) +
                        " job" + ("" if len(cards) == 1 else "s") + " today", "t": now_s, "c": "#2E78C7"})
    del feed[60:]
    state["date"] = dkey

    # hour-by-hour cumulative KPI snapshots for the day: each entry is the
    # state as of the end of that hour; current hour overwrites live until it
    # closes out. Feeds the dashboard HXH tab.
    done_ct = sum(1 for c in cards if c["stages"].get("done"))
    hrs = state.setdefault("hourly", {})
    hrs[str(now.hour)] = {
        "h": now.hour, "jobs": len(cards), "done": done_ct,
        "opts": opt_count, "optsTotal": int(opt_total),
        "signed": len(signed_jobs), "signedTotal": int(signed_total),
        "leads": leads_set_n, "mems": memberships_sold,
    }

    siro_today = None
    if siro_data is not None:
        recorded = sum(1 for t in siro_techs_seen
                       if (_siro_match(t, siro_data) or {}).get("n", 0) >= 1)
        siro_today = {"recorded": recorded, "of": len(siro_techs_seen)}

    # Field Pro rollup for the header chip. Deliberately publishes a COUNT of
    # confirmed recordings and NO denominator: any X/Y framing would silently
    # assert that the Y-X remainder failed to record, and per fact 6 that claim
    # is not supported (a recording can exist without being tied to a job).
    # `unverified` and `pending` are carried separately for diagnosis.
    # null when the report is unavailable — never a fabricated zero.
    rec_today = None
    if fp_data is not None:
        sc = {}
        for c in cards:
            sc[c["rec"]["state"]] = sc.get(c["rec"]["state"], 0) + 1
        rec_today = {"recorded": sc.get("recording", 0),
                     "unverified": sc.get("unverified", 0) + sc.get("none", 0),
                     "pending": sc.get("unknown", 0),
                     "accusing": FP_ACCUSE_OK,
                     # correlator health, for the health pill / diagnosis: how
                     # many of today's confirmed recordings needed correlating,
                     # and how stale its opinion is. null = artifact unavailable,
                     # which means nothing can be accused this cycle.
                     "correlated": sum(1 for c in cards if c["rec"].get("via")),
                     "attribAgeMin": round(fp_attrib["_ageMin"], 1) if fp_attrib else None,
                     "src": "ServiceTitan Field Pro"}

    payload = {
        "date": dkey,
        "day": now.strftime("%A, %B %d").upper(),
        "generated": now_s,
        "generatedMs": int(time.time() * 1000),
        "siroToday": siro_today,
        "recToday": rec_today,
        "kpis": {
            "jobsToday": len(cards),
            "onSiteNow": on_site,
            "optionsInPlayCount": opt_count,
            "optionsInPlayTotal": int(opt_total),
            "signedJobs": len(signed_jobs),
            "signedTotal": int(signed_total),
            "leadsSet": leads_set_n,
            "membershipsSold": memberships_sold,
        },
        "hours": [hrs[k] for k in sorted(hrs, key=int)],
        "jobs": cards,
        "activity": feed,
        "updated": now_s,
    }
    return payload


# ── publish (local only — no git, no cloud) ─────────────────────────────────

def publish(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs", "updated")}
    old_stable = None
    if OUT.exists():
        try:
            oldj = json.loads(OUT.read_text(encoding="utf-8"))
            old_stable = {k: v for k, v in oldj.items() if k not in ("generated", "generatedMs", "updated")}
        except Exception:
            pass
    if stable == old_stable and not force_heartbeat:
        return "unchanged"
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, OUT)
    return "written"


# ── cloud transport: GitHub contents API on the dashboard repo (PAT = DASHBOARD_TOKEN) ──

_GH_SHAS = {}
def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "service-livefeed",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


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


def cloud_seed_state():
    """Start a relay session from the state the previous session committed."""
    txt = gh_fetch("servicefeed_state.json")
    if txt:
        try:
            STATE.write_text(txt, encoding="utf-8")
        except Exception:
            pass
    gh_fetch("servicefeed.json")          # prime the sha cache for the first put


def publish_cloud(payload, force_heartbeat=False):
    stable = {k: v for k, v in payload.items() if k not in ("generated", "generatedMs", "updated")}
    if stable == publish_cloud._last and not force_heartbeat:
        return "unchanged"
    gh_put("servicefeed.json", json.dumps(payload, separators=(",", ":")),
           "Service feed " + payload["generated"])
    gh_put("servicefeed_state.json", STATE.read_text(encoding="utf-8"),
           "Service feed state " + payload["generated"])
    publish_cloud._last = stable
    return "pushed"
publish_cloud._last = None


def arm_next():
    """Queue the successor relay run. GitHub's native cron skips ticks, so every
    session arms its own replacement; the workflow's concurrency group collapses
    extra pending runs."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/johnschwinghamer94-lab/sierra-ropp-hourly/actions/workflows/servicefeed.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
                     "Accept": "application/vnd.github+json", "User-Agent": "service-livefeed",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        log("armed successor relay run")
    except Exception as ex:
        log("WARN: could not arm successor: " + repr(ex)[:150])


def load_state():
    """A MISSING state file is normal (first run / fresh runner). A file that
    exists but won't parse is not: it silently restarts the day — stage times
    reset to checkmarks and the day's hour-by-hour KPI series is lost. Never
    crash the relay for it (the successor would re-crash on the same file and
    the board would stay dark), but make it impossible to miss in the log."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as ex:
        log("STATE CORRUPT: %s exists but did not parse (%s) — starting from an EMPTY "
            "state. Stage times and today's hour-by-hour series are lost. Fix or delete "
            "the file." % (STATE.name, repr(ex)[:150]))
        return {}


def save_state(state):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE)


def cycle(dry=False, force_heartbeat=False):
    state = load_state()
    payload = build(state)
    if dry:
        print(json.dumps(payload, indent=2)[:4000])
        return "dry"
    save_state(state)
    if CLOUD:
        return publish_cloud(payload, force_heartbeat)
    return publish(payload, force_heartbeat)


def in_window(now):
    return (now.hour, now.minute) >= DAY_START and (now.hour, now.minute) <= DAY_END


def cloud_main():
    """One relay session on a GitHub Actions runner (TZ=America/Los_Angeles set
    by the workflow so datetime.now() is Vegas time). Concurrency is handled by
    the workflow's concurrency group — no PID lock here."""
    now = datetime.now()
    if (now.hour, now.minute) < (5, 15) or (now.hour, now.minute) > DAY_END:
        log("outside ops window — session exits")
        return
    fp = Path.home() / ".servicetitan" / "sierra.json"
    if not fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(os.environ["ST_CREDS_JSON"])
    cloud_seed_state()
    log("cloud session started (cap %d min)" % MAX_MIN)
    arm_next()                        # successor waits in the queue from minute one
    # DEAD-MAN SWITCH (2026-07-31): both relays froze mid-cycle at 8:36p when a
    # ServiceTitan call hung without a timeout — sessions sat "running" but
    # silent for hours. A daemon thread hard-exits if no loop iteration
    # completes for 10 min; the queued successor then takes over immediately.
    import threading, socket
    socket.setdefaulttimeout(90)
    _beat = {"t": time.time()}
    def _deadman():
        while True:
            time.sleep(30)
            if time.time() - _beat["t"] > 600:
                log("DEADMAN: no completed loop in 10 min — exiting for the queued successor")
                os._exit(3)
    threading.Thread(target=_deadman, daemon=True).start()
    t0 = time.time()
    last_push = 0.0
    while True:
        now = datetime.now()
        if (now.hour, now.minute) > DAY_END:
            log("past %02d:%02d — session done for today" % DAY_END)
            break
        if (time.time() - t0) / 60 > MAX_MIN:
            log("session cap reached — handing off to the next relay run")
            if (now.hour, now.minute) < DAY_END:
                arm_next()            # belt & suspenders: re-arm on the way out too
            break
        if in_window(now):
            # A cycle counts as FAILED when it raises *or* when it returns a
            # publish-failure sentinel. "Nothing raised" is not the same as
            # "the artifact landed": treating it that way would RESET the
            # consecutive-error counter on a failed publish, so repeated
            # publish failures could never reach the guard below.
            failed = False
            try:
                hb = time.time() - last_push > 240
                r = cycle(force_heartbeat=hb)
                if r == "pushed":
                    last_push = time.time()
                failed = str(r) in PUBLISH_FAILURE_RESULTS
                log(("cycle PUBLISH FAILURE -> " if failed else "cycle -> ") + str(r))
            except Exception as ex:
                log("cycle ERROR: " + repr(ex)[:300])
                failed = True
            # stale-credential zombie guard (8/3): a session holding a revoked
            # token fails every publish without hanging — the dead-man never
            # fires. Bail after 10 straight failures; successor gets fresh secrets.
            _errs = globals().setdefault("_consec_errs", [0])
            if failed:
                _errs[0] += 1
                if _errs[0] >= 10:
                    log("10 consecutive cycle failures — exiting for a fresh session")
                    os._exit(4)
            else:
                _errs[0] = 0
        _beat["t"] = time.time()
        time.sleep(CYCLE_SECS)


def main():
    once = "--once" in sys.argv
    dry = "--dry" in sys.argv
    if dry or once:
        t0 = time.time()
        result = cycle(dry=dry, force_heartbeat=True)
        log("single cycle -> " + str(result) + " (%.1fs)" % (time.time() - t0))
        return
    if CLOUD:
        cloud_main()
        return
    log("loop started (pid %d)" % os.getpid())
    last_push = 0.0
    while True:
        now = datetime.now()
        if (now.hour, now.minute) > DAY_END:
            log("past %02d:%02d — loop done for today" % DAY_END)
            break
        if in_window(now):
            try:
                hb = time.time() - last_push > 240
                r = cycle(force_heartbeat=hb)
                if r == "written":
                    last_push = time.time()
                log("cycle -> " + str(r))
            except Exception as ex:
                log("cycle ERROR: " + repr(ex)[:300])
        time.sleep(CYCLE_SECS)


if __name__ == "__main__":
    main()
