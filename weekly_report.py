"""SILO weekly TGL report — a one-page dashboard-style PDF, week-over-week.

Compares the last full week (Mon-Sun) with the week before, for John's SILO team:
TGLs created / ran / sold / close-rate / canceled, plus a per-tech table.
Run on a Monday (or pass --date YYYY-MM-DD). Writes weekly_report_<end>.pdf.
Needs ServiceTitan creds (ST_CREDS_JSON materialized for cloud).
"""
import os, sys, json
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
from pathlib import Path

_fp = Path.home() / ".servicetitan" / "sierra.json"
if not _fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
    _fp.parent.mkdir(parents=True, exist_ok=True); _fp.write_text(os.environ["ST_CREDS_JSON"])

sys.path.insert(0, str(Path(__file__).parent))
import st_client as st  # noqa: E402
from UPDATE_DASHBOARD import resolve, to_date  # noqa: E402  (same tech-resolve the dashboard uses)
import urllib.error, time  # noqa: E402

# Same reports + logic the ROPP dashboard is built from, so every number matches it.
REP_TGLS, REP_REVENUE = ("technician", 642925621), ("accounting", 379143819)
REP_SCHEDULED, REP_CANCEL = ("technician", 660537364), ("technician", 642928003)

def run_rep(rep, F, T, dt=1):
    for _ in range(8):
        try:
            return st.run_report(rep[0], rep[1], [{"name": "DateType", "value": dt},
                {"name": "From", "value": F}, {"name": "To", "value": T}], page=1, page_size=5000)
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(15); continue
            raise
    raise RuntimeError("report retries")
def _flds(r): return [f["name"] if isinstance(f, dict) else f for f in r["fields"]]
def _vjob(s): s = str(s).strip(); return s.isdigit() and len(s) >= 6

# ---- date range: last full Mon-Sun week vs the one before ----
args = sys.argv[1:]
run = date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args else date.today()
this_mon = run - timedelta(days=run.weekday())
LW = (this_mon - timedelta(days=7), this_mon - timedelta(days=1))     # last week Mon..Sun
PW = (this_mon - timedelta(days=14), this_mon - timedelta(days=8))    # prior week

def week(start, end):
    F, T = start.isoformat(), end.isoformat()
    per = defaultdict(lambda: {"created": 0, "ran": 0, "sold": 0})
    # TGLs created — ROPP TGLS CREATED report, count rows, per creating tech (LeadCreatedBy)
    tc = run_rep(REP_TGLS, F, T); ti = {n: i for i, n in enumerate(_flds(tc))}
    tgls = 0
    for r in tc["data"]:
        if not _vjob(r[ti["JobNumber"]]): continue
        tgls += 1; per[resolve(r[ti["LeadCreatedBy"]]) or "?"]["created"] += 1
    # calls ran — Revenue by Job Type report, count rows
    rv = run_rep(REP_REVENUE, F, T); ri = {n: i for i, n in enumerate(_flds(rv))}
    calls = sum(1 for r in rv["data"] if _vjob(r[ri["JobNumber"]]))
    # ran / same-day / next-day / sold — Scheduled-vs-Ran-vs-Sold report (ran=ScheduledDate)
    sc = run_rep(REP_SCHEDULED, F, T); si = {n: i for i, n in enumerate(_flds(sc))}
    ran = same = nxt = sold = 0
    for r in sc["data"]:
        if not _vjob(r[si["JobNumber"]]): continue
        rd = to_date(r[si["ScheduledDate"]]); cr = to_date(r[si["CreatedDate"]])
        if rd is None: continue
        tech = resolve(r[si["LeadGeneratedFromSourceTech"]]) or "?"
        ran += 1; per[tech]["ran"] += 1
        if cr and rd == cr: same += 1
        if cr and (rd - cr).days == 1: nxt += 1
        if float(r[si["EstimateSalesSubtotal"]] or 0) > 0: sold += 1; per[tech]["sold"] += 1
    # cancellations — ROPP Cancelations report, DateType 2 = cancelled-date; count TGL leads
    # canceled in the week attributed to a lead-gen tech (dashboard method)
    cx = run_rep(REP_CANCEL, F, T, dt=2); ci = {n: i for i, n in enumerate(_flds(cx))}
    canceled = 0
    for r in cx["data"]:
        cd = to_date(r[ci["CancelledDate"]])
        if _vjob(r[ci["JobNumber"]]) and cd and start <= cd <= end and resolve(r[ci["LeadGeneratedBy"]]):
            canceled += 1
    a = {"created": tgls, "calls": calls, "ran": ran, "sold": sold, "canceled": canceled,
         "close": round(sold / ran * 100) if ran else 0,             # Sold / Ran
         "fliprate": round(tgls / calls * 100) if calls else 0,      # TGLs / calls ran (dashboard rate)
         "sameday_rate": round(same / ran * 100) if ran else 0,      # same-day / ran
         "samenext_rate": round((same + nxt) / ran * 100) if ran else 0}
    return a, per

lw, per = week(*LW)
pw, _ = week(*PW)

# ---------- render ----------
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyBboxPatch

INK, MUT, LINE = "#1a2233", "#6b7891", "#e3e8f0"
BLUE, PBLUE, GREEN, RED = "#2E78C7", "#c7d3e6", "#22a95f", "#e63946"
plt.rcParams.update({"font.family": "DejaVu Sans"})
fig = plt.figure(figsize=(8.5, 11), dpi=150)
fig.patch.set_facecolor("white")

def ftxt(x, y, s, size=10, color=INK, weight="normal", ha="left", va="center"):
    fig.text(x, y, s, fontsize=size, color=color, fontweight=weight, ha=ha, va=va)

# header band
fig.add_artist(plt.Rectangle((0, 0.945), 1, 0.055, transform=fig.transFigure, facecolor=BLUE, ec="none"))
ftxt(0.06, 0.9625, "SILO — WEEKLY TGL REPORT", 19, "white", "bold")
ftxt(0.94, 0.9625, "Sierra Air Conditioning & Plumbing", 9.5, "#dbe7f6", ha="right")
mfmt = lambda d: d.strftime("%b %-d")
ftxt(0.06, 0.913, f"Week of {mfmt(LW[0])} – {mfmt(LW[1])}, {LW[1].year}      ·      "
     f"vs. prior week ({mfmt(PW[0])} – {mfmt(PW[1])})", 11.5, MUT)

# KPI tiles
def arrow(dv, good_up=True):
    if dv == 0: return "→", MUT
    up = dv > 0
    return ("▲" if up else "▼"), (GREEN if up == good_up else RED)
def draw_tiles(tset, ty, th):
    m = len(tset); gx0, gx1, gp = 0.06, 0.94, 0.018
    tw = (gx1 - gx0 - gp * (m - 1)) / m
    for i, (lbl, val, dv, gu, suf, sub) in enumerate(tset):
        xx = gx0 + i * (tw + gp)
        ax = fig.add_axes([xx, ty, tw, th]); ax.axis("off"); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
        ax.add_patch(FancyBboxPatch((0.05, 0.06), 0.90, 0.88, boxstyle="round,pad=0.0,rounding_size=0.12",
                                    facecolor="#f6f8fc", edgecolor=LINE, lw=1.1, transform=ax.transAxes))
        ax.text(0.5, 0.83, lbl, ha="center", va="center", fontsize=7.4, color=MUT, fontweight="bold")
        ax.text(0.5, 0.52, f"{val}{suf}", ha="center", va="center", fontsize=21, color=INK, fontweight="bold")
        ar, col = arrow(dv, gu)
        ax.text(0.5, 0.27, f"{ar} {'+' if dv>0 else ''}{dv}{suf}", ha="center", va="center",
                fontsize=8.0, color=col, fontweight="bold")
        if sub: ax.text(0.5, 0.11, sub, ha="center", va="center", fontsize=5.9, color=MUT, style="italic")
row1 = [("TGLs CREATED", lw["created"], lw["created"] - pw["created"], True, "", ""),
        ("SOLD", lw["sold"], lw["sold"] - pw["sold"], True, "", ""),
        ("CLOSE RATE", lw["close"], lw["close"] - pw["close"], True, "%", "Sold ÷ Ran"),
        ("CANCELED", lw["canceled"], lw["canceled"] - pw["canceled"], False, "", "")]
row2 = [("FLIP RATE", lw["fliprate"], lw["fliprate"] - pw["fliprate"], True, "%", "TGLs ÷ calls ran"),
        ("SAME-DAY FLIP", lw["sameday_rate"], lw["sameday_rate"] - pw["sameday_rate"], True, "%", "same-day ÷ ran"),
        ("SAME / NEXT DAY", lw["samenext_rate"], lw["samenext_rate"] - pw["samenext_rate"], True, "%", "same+next ÷ ran")]
draw_tiles(row1, 0.805, 0.082)
draw_tiles(row2, 0.710, 0.082)

# grouped bar: created / ran / sold
axb = fig.add_axes([0.10, 0.455, 0.85, 0.175])
cats = ["Created", "Ran", "Sold"]
pv = [pw["created"], pw["ran"], pw["sold"]]; lv = [lw["created"], lw["ran"], lw["sold"]]
xpos = np.arange(len(cats)); bw = 0.34
axb.bar(xpos - bw/2, pv, bw, label=f"Prior ({mfmt(PW[0])}–{mfmt(PW[1])})", color=PBLUE)
axb.bar(xpos + bw/2, lv, bw, label=f"Last week ({mfmt(LW[0])}–{mfmt(LW[1])})", color=BLUE)
top = max(lv + pv)
for xi, (p, l) in enumerate(zip(pv, lv)):
    axb.text(xi - bw/2, p + top*0.02, str(p), ha="center", fontsize=8.5, color=MUT)
    axb.text(xi + bw/2, l + top*0.02, str(l), ha="center", fontsize=9, color=INK, fontweight="bold")
axb.set_xticks(xpos); axb.set_xticklabels(cats, fontsize=10.5, color=INK)
ftxt(0.10, 0.655, "TGL volume — week over week", 12, INK, "bold", ha="left")
axb.legend(fontsize=8.5, frameon=False, loc="upper right")
for sp in ["top", "right", "left"]: axb.spines[sp].set_visible(False)
axb.tick_params(left=False, labelleft=False, bottom=False); axb.set_ylim(0, top*1.18)
axb.spines["bottom"].set_color(LINE)

# per-tech table (top techs by sold + an "others" roll-up + total)
all_rows = sorted(per.items(), key=lambda kv: (-kv[1]["sold"], -kv[1]["created"]))
Tc = sum(v["created"] for _, v in all_rows); Tr = sum(v["ran"] for _, v in all_rows); Ts = sum(v["sold"] for _, v in all_rows)
TOP = 12
trows = all_rows[:TOP]
ftxt(0.06, 0.405, "By technician — last week", 12, INK, "bold")
ftxt(0.94, 0.406, "Close % = Sold ÷ Ran", 8, MUT, ha="right")
for cx, ct, ha in [(0.06, "TECHNICIAN", "left"), (0.62, "CREATED", "right"), (0.74, "RAN", "right"),
                   (0.85, "SOLD", "right"), (0.94, "CLOSE %", "right")]:
    ftxt(cx, 0.380, ct, 8.0, MUT, "bold", ha=ha)
fig.add_artist(plt.Line2D([0.06, 0.94], [0.370, 0.370], color=LINE, lw=1, transform=fig.transFigure))
ry = 0.350
for tech, v in trows:
    cr, rn, so = v["created"], v["ran"], v["sold"]; cp = round(so / rn * 100) if rn else 0
    ftxt(0.06, ry, tech, 9.6, INK, ha="left")
    ftxt(0.62, ry, str(cr), 9.6, INK, ha="right")
    ftxt(0.74, ry, str(rn), 9.6, MUT, ha="right")
    ftxt(0.85, ry, str(so), 9.6, BLUE, "bold", ha="right")
    ftxt(0.94, ry, f"{cp}%" if rn else "—", 9.6, (GREEN if cp >= 50 else INK), "bold" if cp >= 50 else "normal", ha="right")
    ry -= 0.0198
others = all_rows[TOP:]
if others:
    oc = sum(v["created"] for _, v in others); orn = sum(v["ran"] for _, v in others); os_ = sum(v["sold"] for _, v in others)
    ftxt(0.06, ry, f"+ {len(others)} others", 9.6, MUT, "normal", ha="left")
    ftxt(0.62, ry, str(oc), 9.6, MUT, ha="right"); ftxt(0.74, ry, str(orn), 9.6, MUT, ha="right")
    ftxt(0.85, ry, str(os_), 9.6, MUT, ha="right")
    ftxt(0.94, ry, f"{round(os_/orn*100) if orn else 0}%", 9.6, MUT, ha="right")
    ry -= 0.0198
fig.add_artist(plt.Line2D([0.06, 0.94], [ry + 0.010, ry + 0.010], color=LINE, lw=1, transform=fig.transFigure))
ftxt(0.06, ry - 0.004, "TOTAL", 10, INK, "bold")
ftxt(0.62, ry - 0.004, str(Tc), 10, INK, "bold", ha="right")
ftxt(0.74, ry - 0.004, str(Tr), 10, INK, "bold", ha="right")
ftxt(0.85, ry - 0.004, str(Ts), 10, BLUE, "bold", ha="right")
ftxt(0.94, ry - 0.004, f"{round(Ts/Tr*100) if Tr else 0}%", 10, INK, "bold", ha="right")

ftxt(0.06, 0.036, f"Flip = TGLs ÷ calls ran (Revenue report: {lw['calls']} calls last wk / {pw['calls']} prior).  "
     f"Same-day/Next-day ÷ ran.  Same reports & method as the ROPP dashboard.", 7.4, MUT)
ftxt(0.06, 0.021, f"Generated {run.isoformat()}   ·   ServiceTitan live API   ·   HVAC Service dept — all ROPP TGLs (dept-wide)", 7.4, MUT)

out = Path(__file__).parent / f"weekly_report_{LW[1].isoformat()}.pdf"
fig.savefig(out, facecolor="white")
print("wrote", out)
print(f"created {pw['created']}->{lw['created']} sold {pw['sold']}->{lw['sold']} "
      f"close {pw['close']}%->{lw['close']}% flip {pw['fliprate']}%->{lw['fliprate']}% "
      f"cxl {pw['canceled']}->{lw['canceled']}")
