# Dashboard data audit — 2026-08-03

Audit of everything the ROPP/Service dashboard actually publishes, as of the
`1a935cc` deploy of `sierra-ropp-dashboard` (2026-08-03 11:00 PT).

Scope: all nine published JSON feeds plus the nine data blocks embedded in
`index.html`. Reproduce with:

```
python audit_published_data.py --site /path/to/sierra-ropp-dashboard
```

**19 FAIL · 18 WARN · 12 INFO.** The numbers themselves are in good shape — the
arithmetic inside every block reconciles, and the per-tech rows roll up exactly
to the department totals. What is broken is *delivery*: two pipelines silently
stopped producing, and two blocks group people incorrectly.

---

## What is correct

Worth stating plainly, because most of the payload checks out:

| Check | Result |
|---|---|
| `INITIAL_DATA` — 56 techs, rate/monthly-sum/MTD-tie/svc-maint split | **0 errors** |
| Per-tech YTD sum vs `DEPT_PACE_DATA` | **exact** — 4,662 calls · 2,214 TGLs · $20,602,949 |
| `conv_rate`, `rev_per_tgl`, `expected_pace`, `projected_eoy`, `ahead` | all recompute |
| `MONTHLY_DETAIL` vs `INITIAL_DATA.monthly` | **0 cell disagreements** across 8 months |
| `PACE_DATA` / `ALLTEAMS_DATA` / `SILO_ONLY_DATA` vs `INITIAL_DATA` | **0 disagreements** |
| Close-rate math (`close_rate`=sold/ran, `tgl_pct`=actual/ropps, tgls−canceled=actual) | all hold |
| `hourly.json` cumulative buckets, deltas, tech split, vs `hourly_state.json` | consistent |
| `servicedata` today ≤ wtd ≤ mtd ≤ ytd, closeRate, avgTicket | all hold |
| Live feeds (hourly, livefeed, scorecards, servicefeed, servicecards) | all < 15 min old |

Cross-checks that look like discrepancies but are not: `CANCEL_DATA` YTD cancels
total 512 vs close-rate 365 — 365 is exactly the CA-roster subset, so the two
agree. `livefeed.kpis.jobs`=46 vs `hourly.today.calls`=8 are different metrics
(whole SILO board vs completed ROPP-eligible calls), likewise
`servicefeed.jobsToday`=95 vs `servicedata.today.jobs`=11 (board vs invoiced).

---

## FAIL 1 — the service MTD/YTD panels were frozen for four days, and showed July's MTD through two days of August

`servicedata.json` refreshes `today`/`wtd` on every ~15-minute light run, but
`mtd`/`ytd` only move on the nightly **full** rebuild (5:35 AM PT). From
`servicedata_history.json`:

| Snapshot | today rev | mtd rev | ytd rev |
|---|---|---|---|
| 2026-07-30 23:00 | $33,757 | $969,565 | $4,850,633 |
| 2026-07-31 22:45 | $44,152 | $969,565 | $4,850,633 |
| 2026-08-01 23:46 | $8,574 | **$969,565** | $4,850,633 |
| 2026-08-02 23:01 | $16,997 | **$969,565** | $4,850,633 |
| 2026-08-03 10:48 | $3,820 | $26,093 | $4,936,794 |

MTD and YTD did not move for four consecutive days. On **Aug 1 and Aug 2 the
"month to date" panel was showing July's $969,565** — a full month behind, on a
page whose whole job is telling the department where the month stands. YTD was
understated by $86,161 over the same window.

Root cause is already documented in `service_data.py` ("2026-08-02 incident:
GitHub native cron silently skipped the 5:35 AM full rebuild"), and the
`_light_escalation_reason` guard that fixes it landed in commit `342b8fe` on
2026-08-02 20:57 PT — *after* the bad window. The guard is working: today's
`lastFullRun` is **09:30**, not 05:35, meaning the scheduled full rebuild missed
again this morning and the guard escalated a light run four hours later.

Two things follow. The guard is load-bearing, not a belt-and-braces addition —
the native cron for the full rebuild has now missed at least three consecutive
days. And the guard's `age_h > 26` threshold means a miss is tolerated for up to
26 hours before escalation; on the 1st of a month that is a whole day of
last-month MTD. Tightening the month-boundary case (escalate immediately when
`TODAY.day == 1`, which the `mtdMonth` check already half-covers) would close it.

## FAIL 2 — `weekreview.json` never got last week, and the Monday catch-up cannot recover it

The service week review is missing the week of **2026-07-27 – 08-02**, which
ended yesterday. It holds only `2026-07-20`. The ROPP/SILO side
(`weekreview_silo.json`) has `2026-07-27` but it is still marked `prelim` at
11 AM Monday, so the Monday-morning finalize pass did not run either.

`_maybe_week_review_catchup()` in `service_data.py:993` has a gap:

```python
if is_sun_eve and not entry:
    week_review_main()                       # Sunday: writes the prelim
elif is_mon_am and entry and entry.get("audit") == "prelim":
    week_review_main()                       # Monday: finalizes an existing prelim
```

On Monday the branch requires `entry` to already exist. When the Sunday prelim
never got written — exactly what happened for `weekreview.json` — `entry` is
`None`, both branches fail, and the catch-up logs "nothing to do" forever. **A
missed Sunday run is unrecoverable.** The fix is one condition:

```python
elif is_mon_am and (not entry or entry.get("audit") == "prelim"):
```

Second gap: the catch-up only calls `week_review_main()`, which writes
`weekreview.json`. `weekreview.yml` runs *two* scripts — the second,
`ropp_week_review.py`, produces `weekreview_silo.json` and has no inline
catch-up at all. That is why the SILO week is stuck at `prelim`.

## FAIL 3 — the scorecards tab splits four reps into two or three people each

`scorecards.json` carries 28 cards under **17 distinct name spellings for 12
actual techs**:

| Rep | Spellings published | Cards |
|---|---|---|
| Joe Mendoza | `Joe Mendoza` (4), `Joe_Mendoza` (6) | 10 |
| Nathan Colquitt | `Nathan Colquitt` (1), `Nathan_Colquitt` (2), `Nathan  Colquitt` (1, double space) | 4 |
| David Canales | `David Canales` (1), `David_Canales` (2) | 3 |
| Brandon Moreno | `Brandon Moreno` (1), `Brandon_Moreno` (1) | 2 |

The scorecard view groups by tech name, so Joe Mendoza's 10 cards render as two
separate reps with 4 and 6, and Nathan Colquitt appears three times.

This originates upstream, not in the publish step: `livecards/` in this repo
already holds **22 spellings for 16 techs** — the same underscore and
double-space variants, plus one card whose `tech` is the literal string
`"HVAC"`. Normalizing on write (`re.sub(r"[\s_]+", " ", name).strip()`) fixes
both files; the existing 258 cards need a one-time backfill.

## FAIL 4 — a $1.57M rep is in the combined close-rate table but on neither team

`CLOSE_RATE_DATA.combined` has 16 rows; `team_a` (7) + `team_b` (8) = 15.
**Alex - Oleksiy Yakovchuk** — 287 ROPPs, 121 ran, 66 sold, 54.5% close,
**$1,569,605** — appears in `combined` and in `SILO_ONLY_DATA`, but in neither
team roster.

That single row accounts for every `team_a + team_b ≠ combined` delta the audit
found, exactly:

| Field | team_a+team_b | combined | Δ | Alex's row |
|---|---|---|---|---|
| ropps | 3,183 | 3,470 | +287 | 287 |
| tgls | 1,570 | 1,733 | +163 | 163 |
| ran | 1,203 | 1,324 | +121 | 121 |
| sold | 616 | 682 | +66 | 66 |
| sales | $14,834,890 | $16,404,495 | +$1,569,605 | $1,569,605 |

So the combined view is right and the team views are not wrong so much as
incomplete — but anyone adding the two team panels to sanity-check the total
will come up $1.57M short, and Alex's performance is invisible in the team
breakdown. Either assign him to a team or add an explicit "unassigned" bucket so
the two views reconcile.

---

## WARN — worth a look, not urgent

**Two different figures for August revenue-to-date on the same page.**
`servicedata.budget.monthActualSoFar` = **$20,817**; `servicedata.mtd.revenue` =
**$26,093**. A 20% gap in the same publish. `budget_block()` recomputes from its
own invoice pull on every light run while `mtd` is carried forward from the last
full run, so the budget figure is the *fresher* of the two yet reads $5,276
lower — meaning the two use different revenue definitions, not just different
timestamps. Whichever is right, `todayCommit` is derived from the lower one.

**Seven techs are credited with more TGLs than calls.** Five have TGLs on
literally zero calls: Deshawn Ojeda, Elvin Cruz, Jack Jonathon Vanos, Jose
Huesca, Raul Morales (this one carrying **$49,852**). Plus Xavier Paredes (3
TGLs / 2 calls, $40,592) and Corey Reding (20/19, $86,618). These render as
conversion rates of 105%, 150%, and infinity. The revenue is real and counted in
the $20.6M department total; only the call side of the attribution is missing.

**`data.json` is a stale orphan.** 43 KB of per-tech YTD/MTD numbers, published
at the public repo root, whose `monthly` series **stops at June** and which
disagrees with the live `index.html` on **153 tech/period/field values** (e.g.
Andrew Alonso YTD: 9 calls / $104,888 in `data.json`, 52 calls / $614,972 in
`index.html`). No page fetches it, and it has not been rewritten since the repo
was created — so nothing on the dashboard is wrong because of it, but anyone
hitting the raw URL gets June-era numbers presented as current. Delete it or
regenerate it.

**`CANCEL_DATA` cancel rates are undefined for four techs** — Charles Van Name
(5 cancelled / 0 scheduled), Andy (Gevorg) Madjarian (2/0), Soren Maxwell (1/0),
Daniel Nevarez-Lujan (5/2). Legitimate in itself: `scheduled` is counted on the
scheduled date and `cancelled` on the cancel date, so a job scheduled in 2025
and killed in 2026 lands in one and not the other. But any per-tech cancel rate
built from that pair is meaningless. None of the four are on the SILO roster
that drives the rendered cancel-rate column, so nothing visibly breaks today.

**Three names in `SAMEDAY_DATA` have no `INITIAL_DATA` row** — `Memo (Guillermo)
Hang`, `Martelvious Jones`, and `Marketed Lead` (which is a lead source, not a
person).

---

## INFO — not defects

Ten sub-dollar rounding gaps: `MONTHLY_DETAIL` sums to $5 under
`DEPT_PACE_DATA.ytd_revenue` across 8 months, nine `PACE_DATA` techs are ±$1,
and `CLOSE_RATE monthly.team_b.sales` is $1 off its YTD total. Money is rounded
per month before being summed. Cosmetic.

`DEPT_PACE_DATA.days_elapsed` = 214 where Jan 1 – Aug 3 inclusive is 215; the
pace math excludes the in-progress day, which is the defensible convention and
is applied consistently (`daily_actual` = 20,602,949 / 214 = 96,275 ✓).

---

## Suggested order of work

**Fixed** (see the commits on this branch):

1. ~~**`_maybe_week_review_catchup()`**~~ — the Monday branch now fires when the
   entry is missing entirely, not only when a prelim exists, and the catch-up
   covers `ropp_week_review.py`/`weekreview_silo.json` as well as
   `weekreview.json`. The next light run past Monday 6 AM writes the 7/27 week
   to both tabs.
2. ~~**Name normalization**~~ — done in `tools/merge_livecards.py` rather than at
   the writer, which heals cards already published and keeps working if the
   live-coach writer emits a new variant. No backfill of the 258 livecard files
   is needed: the merge re-reads them all and normalizes carried-forward cards
   too. Verified against the live feed — 17 spellings collapse to 12 techs, card
   count unchanged.

**Still open:**

3. **Assign or bucket Alex - Oleksiy Yakovchuk** so the team views sum to the
   combined total. Needs a call on which team he belongs to (or whether an
   "unassigned" row is the right answer), so it is not a mechanical fix.
4. **Tighten the full-rebuild escalation** at month boundaries so a missed cron
   on the 1st cannot show last month's MTD for a full day.
5. **Reconcile `budget.monthActualSoFar` with `mtd.revenue`**, then delete or
   regenerate `data.json`.
