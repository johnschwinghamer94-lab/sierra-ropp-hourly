#!/usr/bin/env python3
"""Field Pro recording ATTRIBUTION CORRELATOR — closes the gap that keeps the
service dashboard's NOT RECORDING alert disabled.

THE PROBLEM
-----------
Reporting API report 635369650 ("Field Pro Recordings Diagnostics", category
"other") is the only live source for "did this tech record this visit".
Presence of a job-attributed row proves recording. ABSENCE PROVES NOTHING,
because ServiceTitan ties only ~3 of every 4 recording sessions to a job:

    default pull (no TechnicianIds)    -> ONLY job-attributed rows:
        "Recording initiated/stopped from the ServiceTitan Mobile app",
        source "ServiceTitan", JobNumber always populated.
    pull WITH TechnicianIds            -> ALSO the mobile app's own telemetry:
        "Recording started / paused / resumed / interrupted / stopped /
        cancelled", "consentStatusChange", "Deeplink is received",
        "User logged in", "Technician Arrived on the Appointment",
        "Technician Completed the Job".  On 2026-08-04, 42 of 157
        "Recording started" rows (27%) carried a BLANK JobNumber.

So a per-job join says "never recorded" for a quarter of real recordings.
Measured: Aaron Davis job 662208158 has no job-attributed row at all, yet his
technician-filtered stream shows "Technician Arrived on the Appointment" at
21:57:25 and "Recording started" SIXTEEN SECONDS LATER at 21:57:41 under a
blank JobNumber. He was recording.

THE FIX (what this engine does)
-------------------------------
The arrival/completion rows in the technician-filtered stream DO carry a job
number. They define, per technician, a chronological list of JOB WINDOWS. The
blank-job recording events carry a RecordingId, which groups them into
RECORDING SESSIONS with a start and an end. Correlate session -> window by
time and the attribution gap closes.

    visit window  = [ Technician Arrived - LEAD_MIN ,
                      Technician Completed the Job + TRAIL_MIN ]
    session       = all rows sharing one RecordingId (one tech, one recording)
    rule DIRECT        job-attributed recording row exists      -> recording
    rule RECID         session carries a JobNumber on any row   -> recording
    rule WINDOW-START  orphan session STARTS inside a window    -> recording
    rule WINDOW-OVERLAP orphan session merely OVERLAPS a window
                       by >= MIN_OVERLAP_SEC (covers a recorder
                       left running across a visit)             -> recording

Anything the rules cannot place is UNKNOWN, never an accusation.

MEASURED FACTS THIS ENGINE DEPENDS ON (re-measure before "simplifying")
----------------------------------------------------------------------
 1. TIMEZONE TRAP: Timestamp is UTC digits wearing a WRONG "-07:00" suffix.
    Parse the digits as UTC and discard the printed offset. Anchor: job
    671253374 reads 2026-08-05T16:25:24-07:00 and is truly 9:25:24 AM PDT;
    job 669192850's "Recording stopped" reads 2026-08-04T23:53:18-07:00 while
    the job's own completedOn is 2026-08-04T23:53:18.649Z — identical digits.
 2. TechnicianIds is capped at FIVE technicians per call. Over that the API
    answers HTTP 409 with "There are too many technicians; Please select at
    most 5 technicians." — a 409 is NOT always a rate limit here.
 3. Passing TechnicianIds also POPULATES the Technician column (it is the
    literal "Unknown technician" on every row of a default pull) and populates
    RecordingId on the app-telemetry rows. Both are dead weight without it.
 4. From/To filter on the JOB'S COMPLETION DATE and that date is evaluated in
    UTC, not tenant-local. A job completed 6 PM PDT on 8/4 is 01:00Z on 8/5
    and lands in the 8/5 window. Measured: all 32 jobs missing from a
    From=To=2026-08-04 pull had completedOn on 2026-08-05 UTC. The window here
    is therefore [today, today+1] — one local day needs two UTC days.
 5. A job with no completedOn returns NO ROWS AT ALL, so an in-flight job can
    never be judged. It must resolve UNKNOWN, and it does (no completion row
    -> no closed window -> "no-window").

FAIL LOUD, NEVER FABRICATE A ZERO. Any tech whose stream could not be fetched
has every one of that tech's jobs marked UNKNOWN with reason "stream-failed".
A partial artifact is published (the jobs that DID resolve are still useful),
but "ok" goes False and the failure is named in the payload.

Runs in TWO environments (keep servicetitan/ and the sierra-ropp-hourly repo
copies byte-identical):
  local (default): writes fieldpro_attrib.json next to this script.
  cloud (FPATTRIB_CLOUD=1, GitHub Actions): publishes fieldpro_attrib.json to
    the dashboard repo via the GitHub contents API with DASHBOARD_TOKEN, the
    same transport servicefeed_sync.py uses. Creds come from ST_CREDS_JSON.

servicefeed_sync.py reads the artifact and merges it with its own direct
signal; see fieldpro_state() there. The direct signal stays authoritative.

Usage:
    python fieldpro_attrib.py            # one pass for today, publish
    python fieldpro_attrib.py --dry      # one pass, print, do not publish
    python fieldpro_attrib.py --day 2026-08-04 --dry   # replay a closed day
"""
import base64, gzip, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import st_client as st

CLOUD = os.environ.get("FPATTRIB_CLOUD") == "1"
SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "fieldpro_attrib.json"
LOG = SCRIPT_DIR / "fieldpro_attrib_log.txt"
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"

FP_REPORT_CATEGORY = "other"
FP_REPORT_ID = 635369650
MAX_TECH_IDS = 5          # hard API cap (fact 2) — do NOT raise

SERVICE_BU_ID = 333
SERVICE_TEAMS = {"2a Service NO Sam Maintenance": "2A",
                 "2b Service with SAM Maintenance": "2B"}

# ── correlation tuning ──────────────────────────────────────────────────────
# Every one of these is deliberately biased toward FINDING evidence of a
# recording. A too-generous window costs a missed violation; a too-tight one
# costs a public false accusation, and John's standing rule is that the second
# is unacceptable while the first is merely disappointing.
LEAD_MIN = 10             # a tech may start recording before ST logs "Arrived"
TRAIL_MIN = 20            # ...and keep recording while writing the invoice
MIN_OVERLAP_SEC = 30      # ignore a session that only grazes a window
ARRIVAL_EV = "Arrived on the Appointment"
COMPLETE_EV = "Completed the Job"
APP_SOURCE = "Field Pro app"


def log(msg):
    line = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 400_000:
            LOG.write_text("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-1500:]) + "\n",
                           encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fp_local(ts):
    """Report Timestamp -> aware local datetime. The digits are UTC and the
    '-07:00' suffix is WRONG (fact 1): slice it off, label the digits UTC."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)[:19]).replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return None


def fmt_t(dt_local):
    if not dt_local:
        return None
    s = dt_local.strftime("%I:%M:%S%p").lstrip("0")
    return s[:-2] + ("p" if s.endswith("PM") else "a")


def _err_body(e):
    b = e.read()
    try:
        b = gzip.decompress(b)
    except Exception:
        pass
    return b.decode("utf-8", "replace")[:300]


def run_fp_report(frm, to, tech_ids=None, tries=4):
    """One report run, all pages. Raises on final failure — callers decide what
    UNKNOWN that turns into. 409 means EITHER 'too many technicians' (a bug in
    the caller, never retryable) OR the tenant has too many report runs in
    flight (retryable), so the message is inspected rather than guessed at."""
    if tech_ids and len(tech_ids) > MAX_TECH_IDS:
        raise ValueError("at most %d technician ids per call (fact 2)" % MAX_TECH_IDS)
    params = [{"name": "From", "value": frm}, {"name": "To", "value": to}]
    if tech_ids:
        params.append({"name": "TechnicianIds", "value": list(tech_ids)})
    rows, fields, page = [], None, 1
    while page <= 6:                      # safety cap; a 5-tech 2-day pull is ~800 rows
        r = None
        for attempt in range(tries):
            try:
                r = st.run_report(FP_REPORT_CATEGORY, FP_REPORT_ID, params,
                                  page=page, page_size=5000)
                break
            except urllib.error.HTTPError as ex:
                if ex.code != 409:
                    raise
                msg = _err_body(ex)
                if "too many technicians" in msg.lower():
                    raise RuntimeError("technician batch too large: " + msg)
                if attempt == tries - 1:
                    raise RuntimeError("report 409 after %d tries: %s" % (tries, msg))
                time.sleep(8 * (attempt + 1))
        fields = [f["name"] for f in (r.get("fields") or [])] or fields
        rows += r.get("data", [])
        if not r.get("hasMore") or not r.get("data"):
            break
        page += 1
        time.sleep(1)
    # A real Reporting-API response ALWAYS carries a field list, even for an
    # empty window. A missing field list is a malformed 200 body, which would
    # otherwise read as "nobody recorded" — the exact fabricated zero this
    # engine exists to prevent.
    if not fields:
        raise RuntimeError("no field list in the report response")
    ix = {f: i for i, f in enumerate(fields)}
    for need in ("Timestamp", "JobNumber", "Event", "Technician", "RecordingId", "Source"):
        if need not in ix:
            raise RuntimeError("report is missing the %s column" % need)
    return ix, rows


# ── entity-API side: which jobs and which techs are in scope ────────────────

def paged(path, params, tenant="SIE"):
    out, page = [], 1
    while True:
        r = st.api_get(path, dict(params, page=page, pageSize=500), tenant)
        if not r:
            raise RuntimeError("empty response body from " + path)
        out += r.get("data", [])
        if not r.get("hasMore"):
            return out
        page += 1
        time.sleep(0.15)


def chunked_get(path, ids, extra=None):
    out, ids = [], list(ids)
    for i in range(0, len(ids), 50):
        p = dict(extra or {})
        p["ids"] = ",".join(str(x) for x in ids[i:i + 50])
        out += paged(path, p)
        time.sleep(0.15)
    return out


def fetch_roster():
    """{lower(name): team} for active BU-333 techs on the 2A/2B teams. Keyed
    lower-case (the PRUET/Pruet case-twin landmine)."""
    roster = {}
    for t in paged("/settings/v2/tenant/{tenant}/technicians", {}):
        if t.get("active") and t.get("businessUnitId") == SERVICE_BU_ID:
            team = SERVICE_TEAMS.get(t.get("team"))
            if team and (t.get("name") or "").strip():
                roster[t["name"].strip().lower()] = team
    return roster


def fetch_scope(day):
    """(jobs_in_scope, tech_by_job) for one local day, using EXACTLY the
    inclusion rule servicefeed_sync.py's board uses: the job's own business
    unit is 333, or a 2A/2B roster tech is assigned to it.

    jobs_in_scope: {job_number: job dict}
    tech_by_job:   {job_number: {tech_id: tech_name}}
    """
    day0 = datetime.combine(day, datetime.min.time()).astimezone().astimezone(timezone.utc)
    day1 = day0 + timedelta(days=1)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    appts = [a for a in paged("/jpm/v2/tenant/{tenant}/appointments",
                              {"startsOnOrAfter": iso(day0), "startsBefore": iso(day1)})
             if a.get("status") != "Canceled"]
    if not appts:
        return {}, {}
    asg, aids = [], [a["id"] for a in appts]
    for i in range(0, len(aids), 50):
        asg += paged("/dispatch/v2/tenant/{tenant}/appointment-assignments",
                     {"appointmentIds": ",".join(str(x) for x in aids[i:i + 50])})
        time.sleep(0.15)
    by_appt = {}
    for a in asg:
        if a.get("active") and a.get("technicianName"):
            by_appt.setdefault(a["appointmentId"], {})[a["technicianId"]] = a["technicianName"]
    jobs_all = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs",
                                                sorted({a["jobId"] for a in appts}))}
    roster = fetch_roster()
    jobs, tech_by_job = {}, {}
    for a in appts:
        j = jobs_all.get(a["jobId"])
        if not j:
            continue
        techs = by_appt.get(a["id"], {})
        tracked = (j.get("businessUnitId") == SERVICE_BU_ID
                   or any((n or "").strip().lower() in roster for n in techs.values()))
        if not tracked:
            continue
        jn = str(j.get("jobNumber") or "").strip()
        if not jn:
            continue
        jobs[jn] = j
        tech_by_job.setdefault(jn, {}).update(techs)
    return jobs, tech_by_job


# ── the correlator ──────────────────────────────────────────────────────────

def _is_rec_event(ev):
    return "recording" in (ev or "").lower()


def _is_rec_start(ev):
    e = (ev or "").strip().lower()
    return e.startswith("recording started") or e.startswith("recording initiated")


def correlate(ix, rows, batch_techs):
    """Turn one technician-filtered pull into evidence.

    batch_techs: {tech_id: tech_name} for the ≤5 techs this pull was scoped to.
    Rows whose Technician column still reads "Unknown technician" cannot be
    pinned to one tech, so they are scoped to the WHOLE batch — a session like
    that is allowed to satisfy any of the batch's visits. That is deliberately
    generous: an unattributable session must never become an accusation.

    Returns (direct, corr, visits_by_job, stats).
      direct : {job_number: [ {t, ev} ]}          job-attributed recording rows
      corr   : {job_number: [ {rid, rule, t0, t1, tech} ]}  correlated sessions
      visits_by_job : {job_number: [ {tech, arr, end} ]}
      stats  : counters for the residual-risk report
    """
    names = {(n or "").strip() for n in batch_techs.values()}
    ANY = "\x00batch"       # sentinel tech scope: "any tech in this batch"

    def who(r):
        n = str(r[ix["Technician"]] or "").strip()
        return n if n in names else ANY

    direct, arrivals, comps, sessions = {}, {}, {}, {}
    app_rows = {}
    for r in rows:
        ev = str(r[ix["Event"]] or "")
        jn = str(r[ix["JobNumber"]] or "").strip()
        t = fp_local(r[ix["Timestamp"]])
        if t is None:
            continue
        tech, rid = who(r), r[ix["RecordingId"]]
        if str(r[ix["Source"]] or "") == APP_SOURCE:
            app_rows[tech] = app_rows.get(tech, 0) + 1
        if ARRIVAL_EV in ev and jn:
            arrivals.setdefault(tech, []).append((t, jn))
        elif COMPLETE_EV in ev and jn:
            comps.setdefault((tech, jn), []).append(t)
        if _is_rec_event(ev) and jn:
            direct.setdefault(jn, []).append({"t": fmt_t(t), "ev": ev[:60]})
        if rid:
            s = sessions.setdefault(str(rid), {"tech": tech, "job": None, "t0": t, "t1": t,
                                               "start": None, "n": 0})
            s["n"] += 1
            s["t0"] = min(s["t0"], t)
            s["t1"] = max(s["t1"], t)
            if jn and not s["job"]:
                s["job"] = jn
            if tech != ANY:
                s["tech"] = tech
            if _is_rec_start(ev) and (s["start"] is None or t < s["start"]):
                s["start"] = t

    # visit windows: an arrival opens a window, that job's first completion at
    # or after it closes it. No completion -> window stays OPEN and the job is
    # unjudgeable (fact 5) — in-flight jobs can never be flagged.
    visits, visits_by_job = {}, {}
    for tech, arr in arrivals.items():
        arr.sort()
        for t, jn in arr:
            cand = [c for c in comps.get((tech, jn), []) if c >= t]
            end = min(cand) if cand else None
            v = {"tech": tech, "job": jn, "arr": t, "end": end,
                 "w0": t - timedelta(minutes=LEAD_MIN),
                 "w1": (end + timedelta(minutes=TRAIL_MIN)) if end else None}
            visits.setdefault(tech, []).append(v)
            visits_by_job.setdefault(jn, []).append(v)

    def candidates(tech):
        if tech == ANY:
            return [v for vs in visits.values() for v in vs]
        return visits.get(tech, []) + visits.get(ANY, [])

    corr, uncorrelated, orphans = {}, [], 0
    for rid, s in sessions.items():
        if s["job"]:
            # RECID rule: the session names its own job on at least one row.
            corr.setdefault(s["job"], []).append(
                {"rid": rid[:8], "rule": "recid", "tech": s["tech"] if s["tech"] != ANY else "",
                 "t0": fmt_t(s["t0"]), "t1": fmt_t(s["t1"])})
            continue
        orphans += 1
        # A session's ANCHOR is where the recording actually began. Landing the
        # anchor inside a window is the primary rule and it must not be gated on
        # duration: an aborted-then-restarted session can report start and stop
        # in the SAME SECOND (Kevin Rodriguez 8/5 09:25:27, a known-good
        # recording), so a "must overlap by 30s" test alone throws real
        # recordings away and would have flagged him.
        anchor = s["start"] or s["t0"]
        inside, ov = [], []
        for v in candidates(s["tech"]):
            if v["w1"] is None:
                continue
            if v["w0"] <= anchor <= v["w1"]:
                inside.append(v)
                continue
            lo, hi = max(s["t0"], v["w0"]), min(s["t1"], v["w1"])
            if (hi - lo).total_seconds() >= MIN_OVERLAP_SEC:
                ov.append(v)          # recorder left running across this visit
        if not inside and not ov:
            # RESIDUAL RISK. This session could not be placed on any visit. If
            # it contains a genuine start it may BE the recording for one of
            # that tech's jobs (e.g. the tech recorded but tapped "Arrived"
            # late, so the window sits in the wrong place). `owners` is who has
            # to be blinded for it; an unresolvable Technician blinds the whole
            # batch rather than nobody.
            owners = sorted(names) if s["tech"] == ANY else [s["tech"]]
            uncorrelated.append({"rid": rid[:8], "tech": s["tech"] if s["tech"] != ANY else "?",
                                 "t0": fmt_t(s["t0"]), "t1": fmt_t(s["t1"]), "n": s["n"],
                                 "day": s["t0"].date().isoformat(),
                                 "real": bool(s["start"]), "owners": owners})
            continue
        rule = "window-start" if inside else "window-overlap"
        for v in (inside or ov):
            corr.setdefault(v["job"], []).append(
                {"rid": rid[:8], "rule": rule, "tech": v["tech"] if v["tech"] != ANY else "",
                 "t0": fmt_t(s["t0"]), "t1": fmt_t(s["t1"]),
                 "arr": fmt_t(v["arr"]), "end": fmt_t(v["end"]),
                 "amb": len(inside or ov) > 1})
    stats = {"sessions": len(sessions), "orphans": orphans,
             "uncorrelated": len(uncorrelated), "uncorrelatedDetail": uncorrelated,
             "appRows": {k: v for k, v in app_rows.items() if k != ANY}}
    return direct, corr, visits_by_job, stats


# ── artifact build ──────────────────────────────────────────────────────────

def build(day):
    """The full pass for one local day. Returns the artifact dict."""
    frm, to = day.isoformat(), (day + timedelta(days=1)).isoformat()   # fact 4
    jobs, tech_by_job = fetch_scope(day)
    log("scope: %d tracked jobs on %s" % (len(jobs), day.isoformat()))

    # PASS 1 — one unfiltered pull gives the job-attributed rows for EVERY tech
    # at once. Whatever it resolves needs no per-tech call, which is what keeps
    # this engine's report budget small.
    ix, rows = run_fp_report(frm, to)
    direct = {}
    for r in rows:
        jn = str(r[ix["JobNumber"]] or "").strip()
        ev = str(r[ix["Event"]] or "")
        if jn and _is_rec_event(ev):
            direct.setdefault(jn, []).append({"t": fmt_t(fp_local(r[ix["Timestamp"]])), "ev": ev[:60]})
    log("pass 1 (unfiltered): %d rows, %d jobs with a direct recording row" % (len(rows), len(direct)))

    # PASS 2 — only techs holding a COMPLETED job with no direct evidence are
    # worth a technician-filtered call. Early in the day that set is empty and
    # this engine makes exactly one report call.
    need = {}
    for jn, j in jobs.items():
        if jn in direct:
            continue
        if j.get("jobStatus") != "Completed" or not j.get("completedOn"):
            continue          # in flight -> unjudgeable anyway (fact 5)
        for tid, tname in (tech_by_job.get(jn) or {}).items():
            need[tid] = tname
    log("pass 2: %d technician(s) need a filtered pull" % len(need))

    corr, visits_by_job, stats_all, failed = {}, {}, [], {}
    ids = sorted(need)
    for i in range(0, len(ids), MAX_TECH_IDS):
        grp = {tid: need[tid] for tid in ids[i:i + MAX_TECH_IDS]}
        try:
            gix, grows = run_fp_report(frm, to, list(grp))
        except Exception as ex:
            # LOUD: every job of every tech in this batch stays UNKNOWN.
            for tid, tname in grp.items():
                failed[tid] = tname
            log("BATCH FAILED (%s) — those techs' jobs stay UNKNOWN: %s"
                % (", ".join(grp.values()), repr(ex)[:200]))
            continue
        gdirect, gcorr, gvis, gstats = correlate(gix, grows, grp)
        for jn, v in gdirect.items():
            direct.setdefault(jn, []).extend(v)
        for jn, v in gcorr.items():
            corr.setdefault(jn, []).extend(v)
        for jn, v in gvis.items():
            visits_by_job.setdefault(jn, []).extend(v)
        stats_all.append(gstats)
        log("  batch %-42s -> %d rows, %d sessions, %d orphans, %d uncorrelated"
            % (",".join(n[:8] for n in grp.values()), len(grows), gstats["sessions"],
               gstats["orphans"], gstats["uncorrelated"]))
        time.sleep(2)

    failed_names = {(n or "").strip() for n in failed.values()}
    # RESIDUAL-RISK BLINDFOLD: (tech, local date) pairs where a real recording
    # session could not be placed on any visit. Those techs cannot be accused
    # on that date — the unplaced session might be the very recording we would
    # be saying does not exist. Sessions with no genuine start (consent-only /
    # pause-stop tails, typically 0-7 seconds) are app noise and do not blind.
    blind = set()
    for s in stats_all:
        for u in s["uncorrelatedDetail"]:
            if u["real"]:
                for o in u["owners"]:
                    blind.add((o, u["day"]))

    out = {}
    for jn, j in jobs.items():
        techs = tech_by_job.get(jn) or {}
        tnames = ", ".join(sorted(techs.values()))
        base = {"tech": tnames, "job": jn}
        if jn in direct:
            out[jn] = dict(base, v="recording", rule="direct", ev=direct[jn][:6])
            continue
        if jn in corr:
            best = sorted(corr[jn], key=lambda c: 0 if c["rule"] == "recid" else
                          (1 if c["rule"] == "window-start" else 2))[0]
            out[jn] = dict(base, v="recording", rule=best["rule"], ev=corr[jn][:6])
            continue
        if j.get("jobStatus") != "Completed" or not j.get("completedOn"):
            out[jn] = dict(base, v="unknown", why="in-flight")
            continue
        if any(t in failed_names for t in techs.values()) or not techs:
            out[jn] = dict(base, v="unknown", why="stream-failed" if techs else "no-tech")
            continue
        vs = visits_by_job.get(jn) or []
        closed = [v for v in vs if v["w1"] is not None]
        if not closed:
            # No arrival/completion pair in the stream => no window => the
            # report physically cannot tell us whether he recorded (fact 5).
            out[jn] = dict(base, v="unknown",
                           why="no-window" if not vs else "window-open",
                           arr=fmt_t(vs[0]["arr"]) if vs else None)
            continue
        # Telemetry sanity: if this tech's app emitted NOTHING all day, silence
        # is a broken/absent device feed, not proof of a skipped recording.
        app = {}
        for s in stats_all:
            for k, v in s["appRows"].items():
                app[k] = app.get(k, 0) + v
        if not any(app.get((n or "").strip(), 0) for n in techs.values()):
            out[jn] = dict(base, v="unknown", why="no-app-telemetry")
            continue
        v0 = closed[0]
        vday = v0["arr"].date().isoformat()
        hit = [t for t in techs.values() if ((t or "").strip(), vday) in blind]
        if hit:
            out[jn] = dict(base, v="unknown", why="uncorrelated-session",
                           arr=fmt_t(v0["arr"]), end=fmt_t(v0["end"]),
                           note="%s has an unplaced recording session on %s; it could be "
                                "this job's" % (", ".join(hit), vday))
            continue
        out[jn] = dict(base, v="none", rule="window-empty",
                       arr=fmt_t(v0["arr"]), end=fmt_t(v0["end"]),
                       why="arrival+completion in the stream, zero recording events in "
                           "[arr-%dm, done+%dm] and no orphan session overlapping it"
                           % (LEAD_MIN, TRAIL_MIN))

    sessions = sum(s["sessions"] for s in stats_all)
    orphans = sum(s["orphans"] for s in stats_all)
    uncorr = sum(s["uncorrelated"] for s in stats_all)
    uncorr_real = sum(1 for s in stats_all for u in s["uncorrelatedDetail"] if u["real"])
    now = datetime.now().astimezone()
    counts = {}
    for e in out.values():
        k = e["v"] + (":" + e.get("rule", e.get("why", "")) if e.get("rule") or e.get("why") else "")
        counts[k] = counts.get(k, 0) + 1
    return {
        "generated": now.isoformat(timespec="seconds"),
        "generatedMs": int(time.time() * 1000),
        "day": day.isoformat(),
        "window": {"from": frm, "to": to},
        "ok": not failed,
        "failedTechs": sorted(failed.values()),
        "techsQueried": len(need),
        "rules": {"leadMin": LEAD_MIN, "trailMin": TRAIL_MIN,
                  "minOverlapSec": MIN_OVERLAP_SEC, "maxTechIds": MAX_TECH_IDS},
        "residual": {"sessions": sessions, "orphans": orphans, "uncorrelated": uncorr,
                     "uncorrelatedReal": uncorr_real,
                     "uncorrelatedPctOfOrphans": round(100.0 * uncorr / orphans, 1) if orphans else 0.0,
                     "blindfolded": sorted("%s@%s" % b for b in blind),
                     "detail": [x for s in stats_all for x in s["uncorrelatedDetail"]][:60]},
        "counts": counts,
        "jobs": out,
    }


# ── publish ─────────────────────────────────────────────────────────────────

_GH_SHAS = {}
def _gh_req(path, method="GET", body=None):
    url = "https://api.github.com/repos/" + PUB_REPO + "/contents/" + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
        "Accept": "application/vnd.github+json", "User-Agent": "fieldpro-attrib",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def gh_put(path, text, msg):
    try:
        _GH_SHAS[path] = _gh_req(path)["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(text.encode("utf-8")).decode()}
    if _GH_SHAS.get(path):
        body["sha"] = _GH_SHAS[path]
    try:
        j = _gh_req(path, "PUT", body)
    except urllib.error.HTTPError as e:
        if e.code not in (409, 422):
            raise
        _GH_SHAS[path] = _gh_req(path)["sha"]      # sha raced another writer
        body["sha"] = _GH_SHAS[path]
        j = _gh_req(path, "PUT", body)
    _GH_SHAS[path] = j["content"]["sha"]


def publish(payload):
    text = json.dumps(payload, separators=(",", ":"))
    if CLOUD:
        gh_put("fieldpro_attrib.json", text, "Field Pro attribution " + payload["generated"])
        return "pushed"
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, OUT)
    return "written"


def main():
    argv = sys.argv[1:]
    dry = "--dry" in argv
    day = date.today()
    if "--day" in argv:
        day = date.fromisoformat(argv[argv.index("--day") + 1])
    if CLOUD:
        fp = Path.home() / ".servicetitan" / "sierra.json"
        if not fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(os.environ["ST_CREDS_JSON"])
    t0 = time.time()
    try:
        payload = build(day)
    except Exception as ex:
        # Total failure: publish NOTHING. A stale artifact is safe (it is
        # freshness-gated on the consumer side); a truncated one that silently
        # turns "unknown" into "none" is not.
        log("FATAL: attribution pass failed, artifact NOT updated — every job "
            "stays UNKNOWN for the relay: " + repr(ex)[:300])
        sys.exit(1)
    log("counts: " + json.dumps(payload["counts"], sort_keys=True))
    log("residual: %d orphan sessions, %d uncorrelated (%.1f%%)"
        % (payload["residual"]["orphans"], payload["residual"]["uncorrelated"],
           payload["residual"]["uncorrelatedPctOfOrphans"]))
    if not payload["ok"]:
        log("PARTIAL: streams failed for %s — their jobs are UNKNOWN"
            % ", ".join(payload["failedTechs"]))
    if dry:
        # WHOLE payload, not a prefix — a replay is only useful as evidence if
        # every verdict and its provenance is visible.
        print(json.dumps(payload, indent=1))
        log("dry run (%.1fs)" % (time.time() - t0))
        return
    log("publish -> %s (%.1fs)" % (publish(payload), time.time() - t0))


if __name__ == "__main__":
    main()
