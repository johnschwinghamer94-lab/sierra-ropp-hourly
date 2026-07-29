#!/usr/bin/env python3
"""Render SILO weekly rollup pages (per-rep, crossrep, _index) from a JSON spec.

The <style> block and section order are lifted verbatim from an existing
weekly page, so backfilled weeks are byte-comparable in style with
weekly/2026-07-26/. Body prose is passed through as-is: it is already
authored HTML fragments (<q>, <strong>, <em>) written against the SILO
per-rep template in coaching/weekly/prompt-silo.txt.

LOCKED: word bands only. The only numbers a rendered page may carry are the
rep's verified Repair-Won / Option C Handoff rates and plain call counts.

Usage:
    python3 tools/genweekly.py rep   <spec.json>   # -> weekly/<end>/<Rep_Name>.html
    python3 tools/genweekly.py index <spec.json>   # -> weekly/<end>/_index.html
    python3 tools/genweekly.py page  <spec.json>   # -> free-form page (crossrep)
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
REFERENCE = REPO / "weekly" / "2026-07-26" / "Robert_Silinzy.html"

BAND_FOOTNOTE = (
    "Bands: Strong = consistent across calls; Strong on wins = present on "
    "positive-outcome calls, drops on no-moves; Solid = reliably present, not a "
    "standout; Moderate = inconsistent; Weak = rarely or poorly attempted. Quotes "
    "are drawn directly from this tech's transcripts."
)

VALID_BANDS = {"Strong", "Strong on wins", "Solid", "Moderate", "Weak", "—"}


def style_block():
    m = re.search(r"<style>.*?</style>", REFERENCE.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit("reference style block not found: %s" % REFERENCE)
    return m.group(0)


def head(title):
    return "<title>%s</title>\n%s\n" % (title, style_block())


def check_bands(spec):
    """Guard the LOCKED rule: word bands only, from the five-band key."""
    bad = []
    for row in spec.get("scorecard", []):
        if row["band"] not in VALID_BANDS:
            bad.append(row["band"])
    for item in spec.get("strengths", []) + spec.get("gaps", []):
        if item.get("band") and item["band"] not in VALID_BANDS:
            bad.append(item["band"])
    if bad:
        raise SystemExit("invalid band(s), not in the five-band key: %r" % bad)


def render_rep(spec):
    check_bands(spec)
    name = spec["rep"]
    title = "%s — SILO Coaching Report, week of %s" % (name, spec["week_label"])
    out = [head(title), '<div class="wrap">']
    out.append('<div class="nav"><a href="_index.html">&#8592; Weekly index</a></div>')
    out.append("<!--TITLE:%s-->" % title)

    out.append('<div class="hdr">')
    out.append('  <div class="co">Sierra Air Conditioning &amp; Plumbing</div>')
    out.append('  <div class="sub">SILO Field Strategy Guide — Sales Coaching Report</div>')
    out.append(
        '  <div class="meta">Technician: %s | Trade: HVAC SILO | Market: Las Vegas | %s (week of %s)</div>'
        % (name, spec["month_label"], spec["week_label"])
    )
    out.append(
        '  <div class="rates">%s Unique Calls Analyzed • %s Repair-Won Rate • %s Option C Handoff Rate • Guide: SILO Field Strategy</div>'
        % (spec["calls_assessed"], spec["repair_won_rate"], spec["optionc_rate"])
    )
    out.append("</div>\n")

    out.append("<h2>Bottom Line</h2>")
    out.append('<div class="callout">')
    out.append('  <div class="clabel">Bottom Line</div>')
    out.append("  <p>%s</p>" % spec["bottom_line"])
    out.append("</div>\n")

    out.append("<h2>Performance Snapshot</h2>")
    out.append('<div class="snap">')
    for line in spec["snapshot"]:
        out.append("  <p>%s</p>" % line)
    out.append("</div>\n")

    out.append("<h2>Strategy Guide Scorecard</h2>")
    out.append('<div class="tblwrap">')
    out.append("<table>")
    out.append(
        "  <thead><tr><th>SILO Step</th><th>Band</th><th>Supporting Quote (Call #)</th><th>What the calls show</th></tr></thead>"
    )
    out.append("  <tbody>")
    for row in spec["scorecard"]:
        out.append(
            '    <tr><td><strong>%s</strong></td><td><span class="band">%s</span></td><td>%s</td><td>%s</td></tr>'
            % (row["step"], row["band"], row["quote"], row["shows"])
        )
    out.append("  </tbody>")
    out.append("</table>")
    out.append("</div>")
    out.append("<p><em>%s</em></p>\n" % BAND_FOOTNOTE)

    out.append("<h2>Strengths</h2>\n")
    for i, s in enumerate(spec["strengths"], 1):
        out.append("<h3>%d. %s (%s)</h3>" % (i, s["title"], s["band"]))
        out.append("<p>%s</p>\n" % s["body"])

    out.append("<h2>Consistent Gaps</h2>")
    out.append("<p><em>(patterns across many calls)</em></p>\n")
    for i, g in enumerate(spec["gaps"], 1):
        out.append("<h3>Gap %d — %s (%s)</h3>" % (i, g["title"], g["band"]))
        out.append("<p>%s</p>\n" % g["body"])

    sep = spec["separates"]
    out.append("<h2>What Separates Positive Outcomes From No-Moves</h2>")
    out.append("<p>%s</p>" % sep["open"])
    out.append('<ul class="bul">')
    for step in ("Ready", "Welcome", "Assessment", "Decision", "Deliver"):
        out.append("  <li><strong>%s:</strong> %s</li>" % (step, sep["steps"][step]))
    out.append("</ul>")
    out.append('<div class="callout">')
    out.append('  <div class="clabel">Decisive Gap — the %s</div>' % sep["decisive_step"])
    out.append(
        "  <p><strong>Decisive gap — the %s.</strong> %s</p>" % (sep["decisive_step"], sep["decisive"])
    )
    out.append("</div>\n")

    out.append("<h2>Beyond the Guide</h2>\n")
    for b in spec["beyond"]:
        out.append("<h3>%s</h3>" % b["title"])
        out.append("<p>%s</p>\n" % b["body"])

    out.append("<h2>Top 3 Coaching Priorities</h2>")
    out.append("<p><em>(ranked by impact)</em></p>")
    out.append('<ul class="bul">')
    for p in spec["priorities"]:
        out.append("  <li>%s</li>" % p)
    out.append("</ul>")
    out.append("<p><em>%s</em></p>\n" % spec["priorities_note"])

    out.append("<h2>Suggested Coaching-Session Agenda</h2>")
    out.append("<p><em>(hand to tech)</em></p>")
    out.append('<ul class="bul">')
    for a in spec["agenda"]:
        out.append("  <li>%s</li>" % a)
    out.append("</ul>\n")

    out.append("<h2>Appendix — Data Notes &amp; Method</h2>")
    for note in spec["appendix"]:
        out.append("<p><strong>%s</strong> %s</p>" % (note["label"], note["body"]))
        if note.get("items"):
            out.append('<ul class="plain">')
            for it in note["items"]:
                out.append("  <li>%s</li>" % it)
            out.append("</ul>")
    out.append(
        '<p class="foot">%s<br>Sierra Air Conditioning &amp; Plumbing · %s SILO Sales Coaching Report · Page</p>'
        % (spec["prepared_from"], name)
    )
    out.append("\n</div>")
    return "\n".join(out) + "\n"


def render_index(spec):
    title = "Weekly SILO Coaching Rollup — %s to %s" % (spec["week_start"], spec["week_end"])
    out = [head(title), '<div class="wrap">\n']
    out.append("<!--TITLE:%s-->" % title)
    out.append('<div class="hdr">')
    out.append('  <div class="co">Sierra Air Conditioning &amp; Plumbing</div>')
    out.append('  <div class="sub">Weekly SILO Coaching Rollup</div>')
    out.append('  <div class="meta">%s</div>' % spec["meta"])
    out.append('  <div class="rates">%s</div>' % spec["rates"])
    out.append("</div>\n")

    out.append("<h2>Team Report</h2>")
    out.append('<ul class="idxlist">')
    out.append(
        '<li><a href="crossrep.html">Cross-Rep Report — all %d technicians</a>' % len(spec["reps"])
    )
    out.append('<div class="r">%s</div>' % spec["crossrep_blurb"])
    out.append('<div class="r"><strong>Headline finding:</strong> %s</div>' % spec["headline"])
    out.append("</li>")
    out.append("</ul>\n")

    out.append("<h2>Per-Rep Reports</h2>")
    out.append(
        "<p><em>Ranked by Repair-Won rate, then band quality. The Option C handoff is the primary win — a low Repair-Won rate alongside strong handoffs is not a failure.</em></p>"
    )
    out.append('<ul class="idxlist">')
    for i, r in enumerate(spec["reps"], 1):
        out.append("<li>")
        out.append('  <a href="%s.html">%d. %s</a>' % (r["rep"].replace(" ", "_"), i, r["rep"]))
        pills = "".join('<span class="pill">%s</span>' % p for p in r["pills"])
        out.append('  <div class="r">%s</div>' % pills)
        out.append('  <div class="r">%s</div>' % r["bands"])
        out.append('  <div class="r">%s</div>' % r["note"])
        out.append("</li>")
    out.append("</ul>\n")
    if spec.get("foot"):
        out.append('<p class="foot">%s</p>' % spec["foot"])
    out.append("\n</div>")
    return "\n".join(out) + "\n"


def render_page(spec):
    """Free-form page (crossrep / monthly): spec carries pre-authored body HTML."""
    out = [head(spec["title"]), '<div class="wrap">']
    if spec.get("nav"):
        out.append('<div class="nav">%s</div>' % spec["nav"])
    out.append("<!--TITLE:%s-->" % spec["title"])
    out.append(spec["body"])
    out.append("</div>")
    return "\n".join(out) + "\n"


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    mode, specpath = sys.argv[1], sys.argv[2]
    spec = json.loads(pathlib.Path(specpath).read_text(encoding="utf-8"))
    render = {"rep": render_rep, "index": render_index, "page": render_page}[mode]
    html = render(spec)
    out = REPO / spec["out"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("wrote %s (%d bytes)" % (out.relative_to(REPO), len(html)))


if __name__ == "__main__":
    main()
