# SILO Monthly Coaching Rollup — spec

Generates the MONTHLY per-rep + team coaching rollup, one calendar month at a
time, by aggregating the month's already-graded weekly rollups plus
ServiceTitan ground truth. This is a rollup-of-rollups, not a re-grade of raw
transcripts — do not re-score calls that a weekly report already scored.

## LOCKED rules (restated — do not deviate)

- Five word bands only, exactly these five, exact spelling: **Strong**,
  **Strong on wins**, **Solid**, **Moderate**, **Weak**. No numeric scoring of
  any SILO step, ever.
- **Close-rate %** is the only number allowed anywhere in the report (e.g.
  "62% close rate this month"). Call counts and flip counts are also numeric
  by necessity (they are counts, not scores) but no SILO step, band, or
  overall performance is ever expressed as a number.
- The **Option C flip** (tech hands a system-upgrade lead to a Comfort
  Advisor) is the primary win the tech is graded on — not closing the
  replacement themselves.
- **ServiceTitan truth overrides transcript impressions, in both directions.**
  If a transcript reads like a clean flip but `tgl_truth` shows no TGL was
  ever created for that job, it did not count as a flip. If a transcript
  reads ambiguous but `tgl_truth` shows a TGL was created off that tech's
  call, it counts. Truth wins over vibes every time, whichever direction it
  cuts.

## Inputs, in priority order

1. **Weekly rollup reports** under `weekly/<end-date>/` for every week whose
   date range falls inside or bounds the target month (a week straddling a
   month boundary counts for both months it touches). These are the primary
   evidence — they are already evidence-banded and quote-backed; trust their
   per-step bands and gap/strength findings as the starting material. Do not
   re-derive bands from scratch if a week already banded them; merge and
   trend them instead.
2. **Daily plans** under `plans/<date>/` for every date in the target month.
   Use these to corroborate coaching context (what was assigned, what
   priorities were set) and to check whether prior coaching visibly landed.
3. **`tgl_truth/<date>.json`** for every calendar day of the target month.
   This is AUTHORITATIVE for flip counts and close rates — not the weekly
   reports' own tallies, which may run from partial data. For each rep,
   aggregate across every day of the month:
   - **Flips** = count of `tgl_truth` entries (source job number → lead
     record) attributable to that rep's calls across the month.
   - **Close rate** = (flipped + closed) ÷ gradeable calls, computed from the
     month's `tgl_truth` files plus the gradeable-call counts already
     established in the weekly reports. This is the only number that appears
     in the monthly report body.
   If a `tgl_truth/<date>.json` file is missing for a day inside the month,
   note the gap in the Appendix — do not silently treat it as zero.
4. **Raw call transcripts** — last resort, used only to spot-check a
   surprising truth-vs-weekly-report mismatch, or to fill grading gaps for
   any week inside the month that has no weekly report on file. Never used to
   override a `tgl_truth` flip/no-flip determination.

## Output

Write, for the target month `YYYY-MM`:

- `monthly/<YYYY-MM>/<Rep_Name>.html` — one per active rep that month (rep
  name with spaces replaced by underscores, matching the weekly convention).
- `monthly/<YYYY-MM>/crossrep.html` — team month report, cross-rep
  comparison.
- `monthly/<YYYY-MM>/_index.html` — index page linking every rep report plus
  the crossrep report.

All three are self-contained HTML (inline `<style>`, no external assets) and
reuse the exact dark-navy styling already established by the weekly reports
(`weekly/<end-date>/_index.html` is the reference for the CSS to copy):
background `#0d1428`, body text `#e8edfb`, headings `#f5c518`, content wrapped
in a `max-width:900px` centered column. Do not invent a new visual style —
copy the weekly template's `<style>` block verbatim and reuse its component
classes (`.hdr`, `.callout`, `.tblwrap`/table rules, `ul.bul`, etc.).

## Per-rep monthly report — section order

1. **BOTTOM LINE** — one tight paragraph: where this rep landed for the
   month, in plain language, band-first.
2. **MONTH SNAPSHOT** — calls graded (count), close-rate % (the only number
   besides counts), strongest FSG step(s) this month as a word band,
   weakest FSG step(s) this month as a word band.
3. **TREND ACROSS THE WEEKS** — the band trajectory week over week inside the
   month, stated in words: improving / flat / declining, per step where the
   trend is meaningful. Cite which week(s) drove the trend.
4. **TOP RECURRING GAPS** — gaps that showed up in more than one week this
   month, merged rather than repeated per-week; each one evidence-cited back
   to a specific week's quote.
5. **WHAT STUCK** — coaching given in an earlier week that visibly improved a
   later week's calls; name the specific coaching point and the week it
   showed up fixed.
6. **NEXT MONTH'S TOP 3 PRIORITIES** — exactly three, ranked, each tied to a
   gap or trend surfaced above.
7. **APPENDIX** — method and data coverage, stated honestly. Note which weeks
   contributed a weekly report vs. which were graded from raw-transcript
   backfill, any `tgl_truth` gaps, and any weeks with no data at all (e.g.
   "transcripts available from Jul 14; weeks 1–2 graded from transcript
   backfill; tgl_truth missing for Jul 3").

`crossrep.html` mirrors the weekly crossrep report's structure — team-level
close rate, band distribution across reps, top team-wide recurring gaps, and
notable individual trends — scoped to the month instead of the week.
`_index.html` is a simple landing page linking to `crossrep.html` and each
rep's page.

## `coaching.json` contract

Maintain a top-level `"monthly"` array in `coaching.json`, in the same shape
as the existing `"weekly"` array, newest month first, capped at 6 entries.
Every other field in `coaching.json` (`generated`, `date`, `reps`, `skipped`,
`dates`, `history`, `weekly`) is preserved exactly as-is — only add/update the
`monthly` key.

```json
{
  "monthly": [
    {
      "month": "2026-07",
      "label": "July 2026",
      "index": "coaching/monthly/2026-07/_index.html",
      "crossrep": "coaching/monthly/2026-07/crossrep.html",
      "reps": {
        "Rep Name": "coaching/monthly/2026-07/Rep_Name.html"
      }
    }
  ]
}
```

When a new month is added, prepend it (newest first) and drop entries beyond
the 6th so the array never exceeds 6 months.

## SILO PRICE RULE (locked — John, 2026-07-27)
The SILO team does NOT do price transparency — quoting or breaking down replacement
pricing is not part of their process. Therefore:
- NEVER band a tech down, list a gap, or lower a grade because they declined to give
  pricing details or deflected a price question.
- Redirecting a price question to the specialist / Option C path IS the correct
  process and should be credited as such (often the "great" move).
- Coaching output must never tell a SILO tech to be more transparent about price or
  to present pricing breakdowns.
