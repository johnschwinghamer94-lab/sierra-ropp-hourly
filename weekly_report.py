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
from livefeed_sync import paged, chunked_get, parse_utc, SHEET_EXCLUDE  # noqa: E402

_APPT = {}
def appt_offset(lead_id, created_iso):
    """Days between the lead job's first appointment and its creation day.
    0 = same-day flip, 1 = next-day. None = no appointment yet."""
    if lead_id not in _APPT:
        try:
            r = st.api_get("/jpm/v2/tenant/{tenant}/appointments", {"jobId": lead_id, "pageSize": 1})
            d = r.get("data") or []
            _APPT[lead_id] = parse_utc(d[0]["start"]).date() if d else None
        except Exception:
            _APPT[lead_id] = None
    ad = _APPT[lead_id]
    return None if ad is None else (ad - date.fromisoformat(created_iso)).days

ROPP_TAG, ROPP_REMOVED = 962027, 545867780   # "ROPP" tag / "Management Removed ROPP" tag
def ropp_calls_ran(start, end):
    """Calls ran = jobs completed in [start,end] tagged ROPP, excluding those also tagged
    'Management Removed ROPP'. This is the flip-rate denominator (John's definition)."""
    n = 0
    for j in paged("/jpm/v2/tenant/{tenant}/jobs",
                   {"tagTypeIds": ROPP_TAG, "completedOnOrAfter": start, "completedBefore": end}):
        if ROPP_REMOVED not in set(j.get("tagTypeIds") or []):
            n += 1
    return n

# ---- date range: last full Mon-Sun week vs the one before ----
args = sys.argv[1:]
run = date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args else date.today()
this_mon = run - timedelta(days=run.weekday())
LW = (this_mon - timedelta(days=7), this_mon - timedelta(days=1))     # last week Mon..Sun
PW = (this_mon - timedelta(days=14), this_mon - timedelta(days=8))    # prior week

TGL_EXCL = ("iaq", "thermostat", "humidifier", "air scrubber", "duct clean",
            "plumb", "water heater", "water treatment", "costco")
def is_tgl(n):
    if not (n or "").startswith("Estimate"): return False
    low = n.lower(); return True if "tgl" in low else not any(x in low for x in TGL_EXCL)
iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
def utc0(d): return datetime.combine(d, datetime.min.time()).astimezone().astimezone(timezone.utc)

jts = {t["id"]: t.get("name", "") for t in paged("/jpm/v2/tenant/{tenant}/job-types", {})}
emps = {t["id"]: t.get("name", "") for t in paged("/settings/v2/tenant/{tenant}/technicians", {})}

def week(start, end):
    tg = []
    d = start
    while d <= end:
        for lj in paged("/jpm/v2/tenant/{tenant}/jobs",
                        {"createdOnOrAfter": iso(utc0(d)), "createdBefore": iso(utc0(d + timedelta(days=1)))}):
            gls = lj.get("jobGeneratedLeadSource") or {}
            src = gls.get("jobId")
            if not src or not is_tgl(jts.get(lj.get("jobTypeId")) or ""): continue
            # dept-wide: every service tech's Estimate-TGL counts (no SILO-team filter)
            tg.append({"src": str(src), "lead": lj["id"], "date": d.isoformat(),
                       "tech": emps.get(gls.get("employeeId")) or "?"})
        d += timedelta(days=1)
    ljs = {j["id"]: j for j in chunked_get("/jpm/v2/tenant/{tenant}/jobs", [t["lead"] for t in tg])}
    sold = set()
    for e in paged("/sales/v2/tenant/{tenant}/estimates", {"soldAfter": iso(utc0(start))}):
        if e.get("jobId") in ljs and (e.get("soldOn") or ((e.get("status") or {}).get("name") == "Sold")):
            sold.add(e["jobId"])
    sof = lambda lid: (ljs.get(lid) or {}).get("jobStatus")
    bysrc = defaultdict(list)
    for t in tg: bysrc[t["src"]].append(t)
    chosen = []
    for s, g in bysrc.items():
        pool = [x for x in g if sof(x["lead"]) != "Canceled"] or g
        pool.sort(key=lambda x: (x["lead"] not in sold, sof(x["lead"]) != "Completed"))
        chosen.append(pool[0])
    a = {"created": 0, "ran": 0, "sold": 0, "canceled": 0, "sameday": 0, "nextday": 0}
    per = defaultdict(lambda: {"created": 0, "ran": 0, "sold": 0})
    for t in chosen:
        stj = sof(t["lead"]); p = per[t["tech"]]
        a["created"] += 1; p["created"] += 1
        if stj == "Canceled": a["canceled"] += 1; continue
        off = appt_offset(t["lead"], t["date"])              # same-day / next-day appointment
        if off == 0: a["sameday"] += 1
        elif off == 1: a["nextday"] += 1
        if stj != "Completed": continue                      # ran/close metrics need a completed job
        a["ran"] += 1; p["ran"] += 1
        if t["lead"] in sold: a["sold"] += 1; p["sold"] += 1
    a["close"] = round(a["sold"] / a["ran"] * 100) if a["ran"] else 0   # Sold / Ran (CA close-rate math)
    a["sameday_rate"] = round(a["sameday"] / a["created"] * 100) if a["created"] else 0
    a["samenext_rate"] = round((a["sameday"] + a["nextday"]) / a["created"] * 100) if a["created"] else 0
    return a, per

lw, per = week(*LW)
pw, _ = week(*PW)
# flip rate = TGLs created / ROPP calls ran (tagged ROPP, excl. Management-Removed ROPP)
lw["calls"] = ropp_calls_ran(iso(utc0(LW[0])), iso(utc0(LW[1] + timedelta(days=1))))
pw["calls"] = ropp_calls_ran(iso(utc0(PW[0])), iso(utc0(PW[1] + timedelta(days=1))))
lw["fliprate"] = round(lw["created"] / lw["calls"] * 100) if lw["calls"] else 0
pw["fliprate"] = round(pw["created"] / pw["calls"] * 100) if pw["calls"] else 0

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
row2 = [("FLIP RATE", lw["fliprate"], lw["fliprate"] - pw["fliprate"], True, "%", "TGLs ÷ ROPP calls"),
        ("SAME-DAY FLIP", lw["sameday_rate"], lw["sameday_rate"] - pw["sameday_rate"], True, "%", "same-day ÷ created"),
        ("SAME / NEXT DAY", lw["samenext_rate"], lw["samenext_rate"] - pw["samenext_rate"], True, "%", "same+next ÷ created")]
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

ftxt(0.06, 0.036, f"Flip denominator = ROPP-tagged completed calls, excl. Management-Removed ROPP "
     f"({lw['calls']} last wk / {pw['calls']} prior)", 7.6, MUT)
ftxt(0.06, 0.021, f"Generated {run.isoformat()}   ·   ServiceTitan live API   ·   HVAC Service dept — all ROPP TGLs (dept-wide)", 7.4, MUT)

out = Path(__file__).parent / f"weekly_report_{LW[1].isoformat()}.pdf"
fig.savefig(out, facecolor="white")
print("wrote", out)
print(f"created {pw['created']}->{lw['created']} sold {pw['sold']}->{lw['sold']} "
      f"close {pw['close']}%->{lw['close']}% flip {pw['fliprate']}%->{lw['fliprate']}% "
      f"cxl {pw['canceled']}->{lw['canceled']}")
