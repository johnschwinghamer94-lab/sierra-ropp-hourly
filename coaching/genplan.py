#!/usr/bin/env python3
"""Render SILO daily coaching plans (+ the day's _index.html) from a JSON spec.

The <style> block and section structure are lifted verbatim from
coaching/EXEMPLAR_Benjamin_Wyllie.html, per coaching/plan_generation_prompt.md
Step 5. Band values are words only — the close rate is the single number the
generator will emit anywhere in a plan.

Usage:  python3 coaching/genplan.py <spec.json>
"""
import html
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
EXEMPLAR = HERE / "EXEMPLAR_Benjamin_Wyllie.html"

BAND_CLASS = {
    "Strong": "strong",
    "Strong on wins": "wins",
    "Solid": "solid",
    "Moderate": "mod",
    "Weak": "weak",
    "Off-tape": "na",
    "Not observed": "na",
    "Flipped": "strong",
    "Closed": "strong",
    "No-flip": "weak",
    "No-close": "weak",
    "Partial": "na",
}

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
{style}</head><body>
<div class="top"><div class="mk">S</div><b>Sierra&nbsp;·&nbsp;{brand}</b><span class="tag">{tag}</span></div>
<div class="wrap">
"""

# dept-specific labels — SILO values preserved EXACTLY so existing (silo) specs render
# byte-identical output; "service" is the new Service-tech rubric counterpart.
DEPT_BRAND = {"silo": "SILO", "service": "Service"}
DEPT_FOOT_PLAN = {
    "silo": "Sierra · SILO daily coaching plan · %s · Scored on strength bands — close rate is the only number",
    "service": "Sierra · Service daily coaching plan · %s · Scored on strength bands — close rate is the only number",
}
DEPT_FOOT_INDEX = {
    "silo": "Sierra · SILO daily coaching index · %s · Scored on strength bands — close rate is the only number",
    "service": "Sierra · Service daily coaching index · %s · Scored on strength bands — close rate is the only number",
}
DEPT_INDEX_TITLE = {"silo": "SILO Daily Coaching — %s", "service": "Service Daily Coaching — %s"}
DEPT_INDEX_H1 = {"silo": "Silo Techs — %s", "service": "Service Techs — %s"}
DEPT_OUT_DIR = {"silo": "plans", "service": "plans_service"}

LEGEND = """<div class="legend">
  <span class="b strong">Strong</span><span class="b wins">Strong on wins</span><span class="b solid">Solid</span>
  <span class="b mod">Moderate</span><span class="b weak">Weak</span><span class="b na">Off-tape</span>
</div>
"""


def style_block():
    m = re.search(r"<style>.*?</style>", EXEMPLAR.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit("exemplar style block not found")
    return m.group(0)


def band(value):
    """A labelled band pill. Unknown labels fall back to the neutral chip."""
    return '<span class="b %s">%s</span>' % (BAND_CLASS.get(value, "na"), html.escape(value))


def rich(text):
    """Spec text may carry <b>/<i>/<q> inline markup; everything else is escaped."""
    out = html.escape(text or "")
    for tag in ("b", "i", "q", "br"):
        out = out.replace("&lt;%s&gt;" % tag, "<%s>" % tag).replace("&lt;/%s&gt;" % tag, "</%s>" % tag)
    return out.replace("&lt;br&gt;", "<br>")


def sh(no, title, hint=""):
    h = '<span class="hint">%s</span>' % html.escape(hint) if hint else ""
    return '<div class="sh"><div class="no">%s</div><h2>%s</h2>%s</div>' % (no, html.escape(title), h)


def row(label, chip, evidence):
    return ('  <div class="row"><div class="rb">%s</div><div>%s</div>'
            '<div class="ev">%s</div></div>\n' % (rich(label), chip, rich(evidence)))


def render_plan(rep, date, dept="silo"):
    name = rep["name"]
    brand = DEPT_BRAND.get(dept, "SILO")
    parts = [HEAD.format(title="%s — %s Coaching Plan %s" % (html.escape(name), brand, date),
                         style=style_block(),
                         brand=brand,
                         tag="Daily Coaching Plan · %s" % date)]
    a = parts.append

    # hero -----------------------------------------------------------------
    a('<div class="hero">\n  <div class="ey">%s</div>\n  <h1 class="disp">%s</h1>\n'
      '  <p class="lede">%s</p>\n</div>\n\n' % (rich(rep["eyebrow"]), html.escape(name), rich(rep["lede"])))

    # glance ---------------------------------------------------------------
    cr = rep["closeRate"]
    crclass = "grn" if rep.get("closeTone", "") == "good" else ("red" if rep.get("closeTone") == "bad" else "")
    a('<div class="glance">\n')
    a('  <div class="g"><div class="k">Close Rate</div><div class="v %s">%s<small>%s</small></div></div>\n'
      % (crclass, html.escape(cr), rich(rep["closeSub"])))
    a('  <div class="g"><div class="k">Calls Reviewed</div><div class="v">%s<small>%s</small></div></div>\n'
      % (rich(rep["callsCount"]), rich(rep["callsSub"])))
    a('  <div class="g"><div class="k">Strongest Section</div><div class="v grn">%s<small>%s</small></div></div>\n'
      % (rich(rep["strongest"]), rich(rep["strongestBand"])))
    a('  <div class="g"><div class="k">Weakest Section</div><div class="v red">%s<small>%s</small></div></div>\n'
      % (rich(rep["weakest"]), rich(rep["weakestBand"])))
    a('</div>\n')
    a(LEGEND)
    a('<p class="note">%s</p>\n\n' % rich(rep["note"]))

    # 1 verdict ------------------------------------------------------------
    v = rep["verdict"]
    a('<section id="verdict">\n' + sh(1, "The one-line verdict") + '\n')
    a('<div class="decisive">\n  <div class="dh"><div class="k">%s</div><h3>%s</h3></div>\n'
      % (rich(v["kicker"]), rich(v["headline"])))
    a('  <div class="split">\n')
    a('    <div class="c said"><div class="lbl">%s</div><p>%s</p></div>\n'
      % (rich(v.get("saidLabel", "What actually happened")), rich(v["said"])))
    a('    <div class="c should"><div class="lbl">%s</div><p>%s</p></div>\n'
      % (rich(v.get("shouldLabel", "What it means")), rich(v["should"])))
    a('  </div>\n</div>\n</section>\n\n')

    # 2 the day's calls ----------------------------------------------------
    a('<section id="calls">\n' + sh(2, "The day's calls", "outcome per gradeable call") + '\n<div class="rub">\n')
    for c in rep["calls"]:
        a(row(c["title"], band(c["outcome"]), c["note"]))
    a('</div>\n</section>\n\n')

    # 3 rubric -------------------------------------------------------------
    a('<section id="rubric">\n' + sh(3, "Strength by rubric section", "band + the evidence for it") + '\n<div class="rub">\n')
    for sec in rep["sections"]:
        a('  <div class="grp"><span class="gn">%s</span><span class="gb">%s</span></div>\n'
          % (html.escape(sec["name"]), band(sec["band"])))
        for r in sec["rows"]:
            a(row(r["behavior"], band(r["band"]), r["evidence"]))
    a('</div>\n\n')

    # 4 critical actions ---------------------------------------------------
    a('<div class="sh" style="margin-top:26px"><div class="no">4</div><h2>Critical Actions — pass rate</h2></div>\n')
    a('<div class="rub">\n')
    for c in rep["critical"]:
        chip = ('<span class="b pass">Pass</span>' if c["pass"] is True else
                '<span class="b na">Off-tape</span>' if c["pass"] is None else
                '<span class="b fail">Fail</span>')
        a(row(c["name"], chip, c["evidence"]))
    a('</div>\n</section>\n\n')

    # 5 strengths ----------------------------------------------------------
    a('<section id="strengths">\n' + sh(5, "What %s did right — real strength, build on it" % name.split()[0]) + '\n')
    a('<ul class="str">\n')
    for s in rep["strengths"]:
        a('  <li>%s</li>\n' % rich(s))
    a('</ul>\n</section>\n\n')

    # 6 gaps ---------------------------------------------------------------
    a('<section id="gaps">\n' + sh(6, "The three gaps", "each one in %s's own calls" % name.split()[0]) + '\n')
    for g in rep["gaps"]:
        a('<div class="mom">\n  <div class="top2">%s<h4>%s</h4></div>\n' % (band(g["band"]), rich(g["title"])))
        for q in g["quotes"]:
            a('  <div class="q"><b>%s</b>%s</div>\n' % (rich(q["who"]), rich(q["text"])))
        a('  <p class="fixline"><b>The fix</b> %s</p>\n</div>\n' % rich(g["fix"]))
    a('</section>\n\n')

    # 7 three-week plan ----------------------------------------------------
    a('<section id="plan">\n' + sh(7, "Three-week training plan") + '\n<div class="rub">\n')
    for i, w in enumerate(rep["weeks"], 1):
        a('  <div class="row"><div class="rb">Week %d</div><div>%s</div><div class="ev">'
          '<b>Drill:</b> %s<br><b>Proof it worked:</b> %s</div></div>\n'
          % (i, band(w.get("band", "Solid")) if w.get("band") in BAND_CLASS else
             '<span class="b solid">%s</span>' % html.escape(w["label"]),
             rich(w["drill"]), rich(w["proof"])))
    a('</div>\n</section>\n\n')

    # 8 what we owe --------------------------------------------------------
    a('<section id="owe">\n' + sh(8, "What we owe %s" % name) + '\n<ul class="str">\n')
    for o in rep["owe"]:
        a('  <li>%s</li>\n' % rich(o))
    a('</ul>\n</section>\n\n')

    a('<div class="bottom">\n  <div class="eyb">Bottom line</div>\n  <p>%s</p>\n</div>\n\n' % rich(rep["bottom"]))
    a('<div class="foot">%s</div>\n' % (DEPT_FOOT_PLAN.get(dept, DEPT_FOOT_PLAN["silo"]) % date))
    a('</div></body></html>\n')
    return "".join(parts)


def render_index(spec, dept="silo"):
    date = spec["date"]
    brand = DEPT_BRAND.get(dept, "SILO")
    parts = [HEAD.format(title=DEPT_INDEX_TITLE.get(dept, DEPT_INDEX_TITLE["silo"]) % date,
                         style=style_block(),
                         brand=brand,
                         tag="Daily Index · %s" % date)]
    a = parts.append
    a('<div class="hero">\n  <div class="ey">Daily Coaching Plans · %s</div>\n' % date)
    a('  <h1 class="disp">%s</h1>\n' % (DEPT_INDEX_H1.get(dept, DEPT_INDEX_H1["silo"]) % date))
    a('  <p class="lede">%s</p>\n</div>\n' % rich(spec["indexLede"]))
    a(LEGEND)
    a('<p class="note">%s</p>\n\n' % rich(spec["indexNote"]))

    a('<section id="reps">\n' + sh(1, "Reps scored", "click a name for the full plan") + '\n<div class="rub">\n')
    for rep in spec["reps"]:
        fn = rep["name"].replace(" ", "_") + ".html"
        label = '<a href="%s" style="color:var(--navy);text-decoration:none;border-bottom:1.5px solid var(--gold)">%s</a> &nbsp;<b>%s</b>' % (
            html.escape(fn), html.escape(rep["name"]), html.escape(rep["closeRate"]))
        chips = ('Strongest: %s &nbsp; Weakest: %s' % (
            band(rep["strongestBand"]) + " " + html.escape(rep["strongest"]),
            band(rep["weakestBand"]) + " " + html.escape(rep["weakest"])))
        a('  <div class="row"><div class="rb">%s</div><div>%s</div><div class="ev">%s</div></div>\n'
          % (label, chips, rich(rep["headlineGap"])))
    a('</div>\n</section>\n\n')

    a('<section id="skipped">\n' + sh(2, "Recordings not scored") + '\n<div class="rub">\n')
    if spec["skipped"]:
        for s in spec["skipped"]:
            a(row(s["rep"], '<span class="b na">Not scored</span>', s["reason"]))
    else:
        a(row("None", '<span class="b na">—</span>', "Every recording on the day was a gradeable customer call."))
    a('</div>\n</section>\n\n')

    a('<div class="foot">%s</div>\n' % (DEPT_FOOT_INDEX.get(dept, DEPT_FOOT_INDEX["silo"]) % date))
    a('</div></body></html>\n')
    return "".join(parts)


def main():
    spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    date = spec["date"]
    # dept is opt-in via a top-level "dept" key in the spec; absent/"silo" keeps
    # SILO behavior byte-identical to before this key existed.
    dept = spec.get("dept", "silo")
    out_dirname = DEPT_OUT_DIR.get(dept, "plans")
    out = REPO / out_dirname / date
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.html"):
        old.unlink()
    written = []
    for rep in spec["reps"]:
        p = out / (rep["name"].replace(" ", "_") + ".html")
        p.write_text(render_plan(rep, date, dept=dept), encoding="utf-8")
        written.append(p.name)
    (out / "_index.html").write_text(render_index(spec, dept=dept), encoding="utf-8")
    written.append("_index.html")

    # guard: the close rate must be the only number-bearing string in a plan
    for p in sorted(out.glob("*.html")):
        body = re.sub(r"<style>.*?</style>", "", p.read_text(encoding="utf-8"), flags=re.S)
        for bad in re.findall(r"\b\d+\s*/\s*(?:5|170)\b|\b\d+\s*points?\b|grade\s*[:=]", body, re.I):
            print("WARN %s: forbidden score token %r" % (p.name, bad))
    print("%s: wrote %s" % (date, ", ".join(written)))


if __name__ == "__main__":
    main()
