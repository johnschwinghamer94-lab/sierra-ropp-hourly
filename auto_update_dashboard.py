#!/usr/bin/env python3
"""
auto_update_dashboard.py — headless daily rebuild + publish for the live site.

Reuses the block-building logic in UPDATE_DASHBOARD.py, but rebuilds IN PLACE
against the deployed repo's index.html (preserving all UI/code edits) and then
commits + pushes so the GitHub Pages workflow deploys it.

Usage:
  python3 auto_update_dashboard.py            # rebuild, commit, push
  python3 auto_update_dashboard.py --dry      # rebuild to a temp file only (no write to repo, no git)
  python3 auto_update_dashboard.py --no-git   # rebuild + write index.html, but do not commit/push
  python3 auto_update_dashboard.py --date 2026-07-03
"""
import os, re, sys, json, subprocess, argparse, tempfile
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import UPDATE_DASHBOARD as U   # SCRIPT_DIR/REPORTS_DIR live in CLAUDE STUFF; reuse its functions

# Repo location differs per machine (Mac vs Windows). Auto-detect; override with ROPP_REPO env var.
REPO_CANDIDATES = [
    "/Users/johnschwinghamer/Documents/GitHub/sierra-ropp-dashboard",   # macOS
    r"C:\Users\johns\sierra-ropp-dashboard",                            # Windows
]
REPO  = os.environ.get("ROPP_REPO") or next((p for p in REPO_CANDIDATES if os.path.isdir(p)), REPO_CANDIDATES[0])
INDEX = os.path.join(REPO, "index.html")


def build_close_rate(techs, cancel, today):
    """CA close rate per silo tech (YTD + MTD): Sold TGLs / Ran TGLs, CA sales $, $/ROPP.
    A TGL counts as SOLD when Jobs Estimate Sales Subtotal (col 10) > 0 -- verified against
    ServiceTitan's export, where Subtotal>0 exactly equals Closed=True. (Do NOT use
    "Installed"/col 8: that's $0 until the unit is physically installed, so it undercounts.)
    Close Rate denominator = RAN opportunities (TGL estimates that ran), keyed on the
    Scheduled/Ran date (col 6) = ServiceTitan's Opportunity Date. "Actual"/"tgl_pct" still
    come from the TGLs-Created report. Attributed to the silo tech (Source Technician, col 4)."""
    mo_name = U.MONTH_NAMES[today.month - 1]
    opp = {}   # name -> {'yr','mr'}          ran opportunities (ytd/mtd)
    sold = {}  # name -> {'yc','ya','mc','ma'}  sold count + amount (ytd/mtd)
    NM = today.month
    tset = {"team_a": set(U.TEAM_A), "team_b": set(U.TEAM_B), "combined": set(U.SILO)}
    tmo = {k: [{"ran": 0, "sold": 0, "sales": 0.0} for _ in range(NM)] for k in tset}  # team monthly history
    for src, rd, sold_amt in U.tgl_estimates():   # ESTIMATE AC/TGLS report (fallback: Scheduled)
        if not src or rd is None or rd.year != U.YEAR:
            continue
        m = rd.month
        o = opp.setdefault(src, {'yr': 0, 'mr': 0}); o['yr'] += 1
        if m == today.month:
            o['mr'] += 1
        for k, members in tset.items():                     # team monthly (ran always; sold when subtotal>0)
            if m <= NM and src in members:
                b = tmo[k][m - 1]; b["ran"] += 1
                if sold_amt > 0:
                    b["sold"] += 1; b["sales"] += sold_amt
        if sold_amt <= 0:
            continue
        e = sold.setdefault(src, {'yc': 0, 'ya': 0.0, 'mc': 0, 'ma': 0.0})
        e['yc'] += 1; e['ya'] += sold_amt
        if m == today.month:
            e['mc'] += 1; e['ma'] += sold_amt

    def metrics(n, period):
        t = techs.get(n); s = sold.get(n, {}); o = opp.get(n, {})
        if period == "ytd":
            ropps = t["ytd"]["calls"] if t else 0
            tgls  = t["ytd"]["tgls"]  if t else 0
            canceled = cancel[n]["ytd"] if n in cancel else 0
            sc = s.get('yc', 0); sa = round(s.get('ya', 0.0)); ran = o.get('yr', 0)
        else:
            ropps = t["mtd"]["calls"] if t else 0
            tgls  = t["mtd"]["tgls"]  if t else 0
            canceled = cancel[n]["monthly"].get(mo_name, 0) if n in cancel else 0
            sc = s.get('mc', 0); sa = round(s.get('ma', 0.0)); ran = o.get('mr', 0)
        actual = max(tgls - canceled, 0)
        return {"ropps": ropps, "tgls": tgls, "canceled": canceled, "actual": actual,
                "tgl_pct": U.rate(actual, ropps), "ran": ran, "sold": sc,
                "close_rate": U.rate(sc, ran), "sales": sa, "per_ropp": round(sa / ropps) if ropps else 0}

    def row(n):
        return {"name": n, "ytd": metrics(n, "ytd"), "mtd": metrics(n, "mtd")}

    def tot(rows, p):
        R = sum(x[p]["ropps"] for x in rows);  A = sum(x[p]["actual"] for x in rows)
        RN = sum(x[p]["ran"] for x in rows)
        S = sum(x[p]["sold"] for x in rows);   SL = sum(x[p]["sales"] for x in rows)
        return {"ropps": R, "tgls": sum(x[p]["tgls"] for x in rows), "canceled": sum(x[p]["canceled"] for x in rows),
                "actual": A, "ran": RN, "sold": S, "close_rate": U.rate(S, RN), "tgl_pct": U.rate(A, R),
                "sales": SL, "per_ropp": round(SL / R) if R else 0}

    def mo_series(k):
        return [{"month": U.MONTH_NAMES[i], "ran": b["ran"], "sold": b["sold"],
                 "cr": U.rate(b["sold"], b["ran"]), "sales": round(b["sales"])}
                for i, b in enumerate(tmo[k])]
    monthly = {"months": U.MONTH_NAMES[:NM], "team_a": mo_series("team_a"),
               "team_b": mo_series("team_b"), "combined": mo_series("combined")}

    ta = [row(n) for n in U.TEAM_A]
    tb = [row(n) for n in U.TEAM_B]
    comb = [row(n) for n in U.SILO]   # Alex + Team A + Team B
    return {"month": mo_name, "team_a": ta, "team_b": tb, "combined": comb, "monthly": monthly,
            "totals": {p: {"team_a": tot(ta, p), "team_b": tot(tb, p), "combined": tot(comb, p)}
                       for p in ("ytd", "mtd")}}


def patch_cancel_markup(html):
    """Ensure the Cancellation tab renders the duplicate-exclusion asterisk + footnote.
    Idempotent and applied every build. Defined HERE (next to its only caller) so it can
    never orphan the call the way a cross-module reference did. Operates on the compiled
    React.createElement output; silently no-ops if the anchor structure changes."""
    if 'label: "YTD CANCELLED *"' not in html:
        html = html.replace('label: "YTD CANCELLED",', 'label: "YTD CANCELLED *",', 1)
    if "CANCEL_DATA.dupNote" not in html:
        anchor = ('    color: "#E63946"\n  })), /*#__PURE__*/React.createElement("div", {\n'
                  '    style: {\n      padding: "14px 24px 0"')
        inject = ('    color: "#E63946"\n  })), '
                  '/*#__PURE__*/(CANCEL_DATA.dupNote ? React.createElement("div", {\n'
                  '    style: { fontSize: 11, color: "#8b98b5", padding: "6px 24px 0", fontStyle: "italic" }\n'
                  '  }, CANCEL_DATA.dupNote) : null), '
                  '/*#__PURE__*/React.createElement("div", {\n    style: {\n      padding: "14px 24px 0"')
        if anchor in html:
            html = html.replace(anchor, inject, 1)
    return html


def build_html(template_html, today):
    """Apply all data blocks to template_html (a full index.html) and return the new html."""
    de = (today - U.YEAR_START).days
    months = U.MONTH_NAMES[:today.month]
    techs, cancel, sd = U.parse_all(today)
    names = [n for n in techs if "," not in n]
    AT = U.build_allteams(techs)
    at_s, at_n, at_a = U.alltotals(AT)
    blocks = {
        "INITIAL_DATA":     U.build_initial(techs),
        "CANCEL_DATA":      U.build_cancel(techs, cancel, today, months),
        "SAMEDAY_DATA":     U.build_sameday(sd, today, months, names),
        "WEEKLY_DATA":      U.build_weekly_prevmonth(today),
        "WEEKLY_CONV_DATA": U.build_weekly_conv(today, names),
        "PACE_DATA":        U.build_pace(techs, today, de),
        "MONTHLY_DETAIL":   U.build_monthly_detail(techs, months),
        "ALLTEAMS_DATA":    AT,
        "SILO_ONLY_DATA":   U.build_silo_only(techs, cancel, sd),
        "YESTERDAY_DATA":   U.build_yesterday(today),
        "DEPT_PACE_DATA":   U.build_dept_pace(at_a, de),
        "CLOSE_RATE_DATA":  build_close_rate(techs, cancel, today),
    }
    html = template_html
    for nm, val in blocks.items():
        html = U.replace_const(html, nm, val)
    html = U.replace_raw(html, "ALLTEAMS_SILO_TOT", U.js_obj(at_s))
    html = U.replace_raw(html, "ALLTEAMS_NS_TOT",   U.js_obj(at_n))
    html = U.replace_raw(html, "ALLTEAMS_ALL_TOT",  U.js_obj(at_a))
    html = U.replace_list(html, "SILO_12", U.SILO_12)
    html = U.replace_list(html, "DEPT_TECHS", U.dept_techs_list(techs))
    html = U.patch_sets(html)
    html = patch_cancel_markup(html)   # asterisk + duplicate-exclusion footnote on the Cancellation tab
    cur = today.strftime("%b ") + str(today.day) + ", " + str(U.YEAR)
    html = re.sub(r'Jan 1 [-–] [A-Z][a-z]{2} \d{1,2}, \d{4}', "Jan 1 – " + cur, html)
    return html, at_a


def git(*args, check=True):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, check=check)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dry", action="store_true", help="write to a temp file only; no repo write, no git")
    ap.add_argument("--no-git", action="store_true", help="write index.html but do not commit/push")
    a = ap.parse_args()
    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else date.today()

    # Live-API source: pull the ROPP reports straight from the ServiceTitan Reporting API
    # (ropp_live) instead of the emailed Excel files. Opt-in via ROPP_SOURCE=api so the
    # Excel path stays the default/fallback. ropp_live monkeypatches U.load_rows.
    if os.environ.get("ROPP_SOURCE") == "api":
        import ropp_live
        U.load_rows = ropp_live.load_rows
        print("Data source: ServiceTitan Reporting API (ropp_live)")

    if not a.dry and not a.no_git:
        git("pull", "--rebase", "--autostash", check=False)

    template = open(INDEX, encoding="utf-8").read()
    html, at_a = build_html(template, today)
    print(f"Rebuilt for {today}: DEPT YTD {at_a['ytd_c']} calls / {at_a['ytd_t']} TGLs / {at_a['ytd_rate']}%")

    if a.dry:
        out = os.path.join(tempfile.gettempdir(), "index_candidate.html")
        open(out, "w", encoding="utf-8").write(html)
        print("DRY RUN — wrote candidate to", out)
        return

    open(INDEX, "w", encoding="utf-8").write(html)
    print("Wrote", INDEX)
    if a.no_git:
        print("NO-GIT — skipped commit/push")
        return

    if not git("diff", "--quiet", "index.html", check=False).returncode:
        print("No data changes; nothing to publish.")
        return
    git("add", "index.html")
    git("commit", "-m", f"Auto-update dashboard data ({today.isoformat()})")
    # Push; if another machine pushed first (both-machines setup), rebase and retry once.
    if git("push", "origin", "main", check=False).returncode:
        git("pull", "--rebase", "--autostash", check=False)
        if git("push", "origin", "main", check=False).returncode:
            print("Push failed after retry (another run may have already published today).")
            return
    print("Pushed.")


if __name__ == "__main__":
    main()
