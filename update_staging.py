#!/usr/bin/env python3
"""Splice freshly-built CA_SALES_DATA / SERVICE_DATA (and the ROPP gate-tile /
PACE_DATA consts, copied verbatim from the current prod index.html) into the
STAGING dashboard pages (test2.html + test_service.html), plus refresh the
LIVE_ROPP footer badge and the staging footer timestamp. No hardcoded dates —
everything derives from today (America/Los_Angeles) and the live cache files.

Usage:  python update_staging.py
Requires: ca_live.py / service_live.py caches already refreshed (staging_fetch.py),
and cache/estimate.json + cache/tgls_created.json current (owned by the prod
15-min pipeline — read only, never refetched here).
"""
import json, os, re, sys, datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ca_live, service_live
import UPDATE_DASHBOARD as U   # for jobkey()

REPO = os.environ.get("STAGING_DASHBOARD_REPO", r"C:\Users\johns\sierra-ropp-dashboard")
TARGETS = ["test2.html", "test_service.html"]
TZ = ZoneInfo("America/Los_Angeles")


def _dump(D):
    s = json.dumps(D, separators=(",", ":"))  # ensure_ascii=True by default
    assert "</script" not in s.lower(), "unsafe </script sequence in JSON payload"
    return s


def _replace_const(html, name, value_json, path_label):
    pat = re.compile(r"^const " + re.escape(name) + r" = .*;$", re.MULTILINE)
    n = len(pat.findall(html))
    if n != 1:
        if n == 0:
            print(f"  {path_label}: no 'const {name}' — page doesn't carry that deck, skipping")
            return html
        raise RuntimeError(f"{path_label}: expected exactly 1 occurrence of 'const {name} = ...;', found {n}")
    return pat.sub(lambda m: f"const {name} = {value_json};", html, count=1)


def _tgl_headline():
    """Same calibrated ROPP TGL methodology as ropp_live.py's ROPP_Estimate_TGLs
    config: estimate rows whose source-lead job number is one of the TGLs-Created
    job numbers. Reads the current prod caches (owned by the 15-min pipeline)."""
    tgls = json.load(open(os.path.join(HERE, "cache", "tgls_created.json")))
    jt = tgls["fields"].index("JobNumber")
    tgl_set = {U.jobkey(r[jt]) for r in tgls["rows"] if U.jobkey(r[jt])}
    est = json.load(open(os.path.join(HERE, "cache", "estimate.json")))
    F = est["fields"]
    iSrcJob = F.index("LeadGeneratedFromSourceJobNumber")
    iSub = F.index("EstimateSalesSubtotal")
    rows = [r for r in est["rows"] if U.jobkey(r[iSrcJob]) in tgl_set]
    rev = round(sum(float(r[iSub] or 0) for r in rows))
    today = dt.datetime.now(TZ).date()
    year = today.year
    mon_abbr = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return {
        "tgl_revenue": rev,
        "tgl_count": len(rows),
        "range": f"Jan 1 \u2013 {mon_abbr[today.month-1]} {today.day}, {year}",
        "fetched": dt.datetime.now(TZ).strftime("%b %d %I:%M %p").replace(" 0", " "),
        "source": "ServiceTitan Reporting API \u2014 matches uploaded reports",
    }


def _index_const_line(name):
    idx_path = os.path.join(REPO, "index.html")
    html = open(idx_path, encoding="utf-8").read()
    pat = re.compile(r"^const " + re.escape(name) + r" = .*;$", re.MULTILINE)
    m = pat.findall(html)
    if len(m) != 1:
        raise RuntimeError(f"index.html: expected exactly 1 occurrence of 'const {name} = ...;', found {len(m)}")
    return m[0]  # full "const NAME = {...};" line, ready to splice in verbatim


def main():
    print("Building CA_SALES_DATA + SERVICE_DATA from refreshed caches...")
    CA = ca_live.build()
    SV = service_live.build()
    ca_json = _dump(CA)
    sv_json = _dump(SV)

    print("Pulling current PACE_DATA / DEPT_PACE_DATA verbatim from prod index.html...")
    pace_line = _index_const_line("PACE_DATA")
    dept_pace_line = _index_const_line("DEPT_PACE_DATA")

    print("Computing fresh ROPP TGL headline (calibrated, from live prod caches)...")
    live_ropp = _tgl_headline()
    live_ropp_json = _dump(live_ropp)

    now_str = dt.datetime.now(TZ).strftime("%b %d %Y %I:%M %p PT")

    for fname in TARGETS:
        path = os.path.join(REPO, fname)
        html = open(path, encoding="utf-8").read()
        orig_len = len(html)

        html = _replace_const(html, "CA_SALES_DATA", ca_json, fname)
        html = _replace_const(html, "SERVICE_DATA", sv_json, fname)

        # PACE_DATA / DEPT_PACE_DATA: replace whole line verbatim from index.html
        pat_pace = re.compile(r"^const PACE_DATA = .*;$", re.MULTILINE)
        _n = len(pat_pace.findall(html))
        if _n > 1:
            raise RuntimeError(f"{fname}: PACE_DATA const found {_n} times")
        if _n == 1:
            html = pat_pace.sub(lambda m: pace_line, html, count=1)

        pat_dept = re.compile(r"^const DEPT_PACE_DATA = .*;$", re.MULTILINE)
        _n = len(pat_dept.findall(html))
        if _n > 1:
            raise RuntimeError(f"{fname}: DEPT_PACE_DATA const found {_n} times")
        if _n == 1:
            html = pat_dept.sub(lambda m: dept_pace_line, html, count=1)

        # LIVE_ROPP footer badge
        pat_live = re.compile(r"^window\.LIVE_ROPP = .*;$", re.MULTILINE)
        _n = len(pat_live.findall(html))
        if _n > 1:
            raise RuntimeError(f"{fname}: window.LIVE_ROPP found {_n} times")
        if _n == 1:
            html = pat_live.sub(lambda m: f"window.LIVE_ROPP = {live_ropp_json};", html, count=1)

        # STAGING footer timestamp (both "STAGING · test.html" occurrences)
        html = html.replace("STAGING \u00b7 test.html",
                             f"STAGING \u00b7 refreshed {now_str}")

        new_len = len(html)
        ratio = new_len / orig_len
        if not (0.8 <= ratio <= 1.2):
            raise RuntimeError(f"{fname}: size changed by more than 20% ({orig_len} -> {new_len})")

        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  {fname}: {orig_len} -> {new_len} bytes ({ratio*100:.1f}%)")

    print("Done.")


if __name__ == "__main__":
    main()
