<!-- Service-rubric counterpart to coaching/plan_generation_prompt.md, created 2026-08-04.
Mirrors that prompt's structure/steps exactly, adapted for Service techs (dispatch teams
2A/2B). Rubric source: "LIVE COACH/scoring_prompt_service.txt" (welcome / diagnosis /
options / close bands, 4 critical actions, outcome closed/no-close/unknown). Output goes
to plans_service/<date>/ (NOT plans/) and service_coaching.json at the repo root (NOT
coaching.json) so the SILO index and dashboard feed are never touched by this routine.
CLOUD PATH NOTES: transcripts live in transcripts/<date>/ in this repo, filtered to
svc__-prefixed files only; scorecards will live in scorecards_full/<date>/ as
svc__-prefixed *.md cards once the cloud service-scoring routine exists — until then all
svc__ calls are "uncarded" and the transcript fallback applies to everything, same as the
SILO prompt's bootstrap period. service_coaching.json plus plans_service/ are relayed to
the public dashboard repo by .github/workflows/coaching_relay.yml, which fires on push to
plans_service/** or service_coaching.json. -->


You are generating daily coaching plans for Sierra Air Conditioning & Plumbing's SERVICE
techs (dispatch teams 2A/2B). This is the SERVICE-rubric counterpart to the SILO plan
generation routine — it must never touch plans/, coaching.json, or any SILO output.

**Standing rule (John, locked):** Service techs are NOT scored on flipping or creating a
TGL lead — that is a SILO-only concept. NEVER use the words "flip," "TGL," or "Option C"
anywhere in a Service plan. Service techs are scored on how well they diagnose the
problem and sell the repair / good-better-best upgrade in front of them.

## Paths (this repo = REPO)
- Rubric: REPO/coaching/service_plan_prompt.md (this file) — mirrors
  REPO/coaching/scoring_prompt_service_cloud.md, the live-scoring source of truth for
  band/critical-action definitions. Read that file too and stay consistent with its
  wording.
- HTML template: reuse REPO/coaching/EXEMPLAR_Benjamin_Wyllie.html's exact <style> block
  and class structure via REPO/coaching/genplan.py's service-plan renderer (same script,
  service mode) — do not hand-roll different CSS.
- Transcripts root: REPO/transcripts/<date>/ — ONLY process files whose name starts with
  the "svc__" prefix (or whose METADATA/header identifies them as Service-roster reps);
  ignore every other transcript in that folder, they belong to the SILO pipeline.
- Scorecards root: REPO/scorecards_full/<date>/ — ONLY the svc__-prefixed cards, once
  the cloud Service scoring routine exists.
- Output root: REPO/plans_service/<date>/
- Service roster: REPO/coaching/service_roster.txt (25 techs, teams 2A/2B; SILO reps are
  deliberately excluded — never generate a plan for a name in coaching/silo_roster.txt).

## Step 0 — Determine which day to process (DO NOT trust the environment date)
The date shown in your context can be stale, so compute it yourself with bash in Las
Vegas local time (this is macOS-adjacent tooling — use -v, not -d, if run on macOS; the
cloud routine runs on Linux, so `date -d` there):
  TZ=America/Los_Angeles date +%F          # "today" locally
  TZ=America/Los_Angeles date -d "-1 day" +%F   # normal target = yesterday
Resolve the TARGET date as follows:
  1. Normal case: TARGET = yesterday (local). Use it only if transcripts/TARGET/ exists
     AND contains at least one svc__-prefixed, non-empty .txt AND plans_service/TARGET/
     does NOT already exist (if it does, this day is done — fall through to rule 2).
  2. Catch-up case: otherwise, scan the transcripts/ folders named YYYY-MM-DD, ignore any
     starting with "_", restrict to the LAST 3 DAYS (yesterday and the two days before
     it), and pick the OLDEST such folder that (a) has at least one svc__-prefixed .txt
     and (b) does NOT already have a matching output folder under plans_service/. Process
     that day.
     OLDEST, not most recent — this is deliberate. Picking the most recent orphans any
     skipped day permanently: on 2026-08-06 the run picked 2026-08-05 and 2026-08-04 was
     lost for good even though its 66 svc__ transcripts were sitting right there. Oldest
     first means one missed day is healed by the very next run. The 3-day window is the
     backstop that keeps a long gap from dragging the routine into ancient history and
     falling permanently behind the current day.
  3. If no such folder exists there is nothing to GRADE — but do NOT stop yet. First run
     the Step 6b newest-day reconciliation: if service_coaching.json's top-level `date` is
     older than the newest date folder in plans_service/, rebuild that file from the newest
     day and push it. Then report "No unprocessed Service transcript day found — nothing to
     do." Do not create empty plan output.
State clearly which TARGET date you resolved and why. The output folder is named for the
TARGET (transcript) date: plans_service/TARGET/.

## Step 1 — Load reference files
Read coaching/scoring_prompt_service_cloud.md in full (the live-scoring rubric this plan
must stay consistent with) and coaching/EXEMPLAR_Benjamin_Wyllie.html in full (structure
only — the SERVICE plan swaps in Service section names, see Step 5).

## Step 2 — Load scorecards FIRST (primary input), raw transcripts only as fallback
If a live-coach Service scoring routine has been scoring calls during the day, its cards
land at scorecards_full/TARGET/svc__*.md (also check scorecards_full/TARGET+1/ — evening
calls after ~5 PM file under the next UTC date). These cards are the PRIMARY analysis
input — do NOT re-read raw transcripts for calls that already have a card. Quotes in a
card's Evidence lines are verbatim from the transcript and may be used directly.

Fallback ONLY for uncarded calls: list transcripts/TARGET/, filter to svc__-prefixed
files only, and match each transcript (rep + Job # / customer in the filename) against
any existing cards. For transcripts with no matching card, read the raw .txt. If a needed
quote for a card-backed call isn't in the card, you may open that one call's transcript —
but only that one.

## Step 3 — Triage every recording
Card-backed calls were already screened during scoring — take their bands/outcome as
given. Apply this triage to the FALLBACK transcripts (uncarded calls) you read:
- SCORE: genuine in-home customer service calls where a tech is diagnosing/repairing for
  a homeowner/decision-maker.
- SKIP (list with one-line reason): team training sessions, ride-alongs with no customer,
  driving/commute recordings, internal chatter, empty/near-empty recordings, and calls
  under ~5 minutes with no real customer dialogue.
- Speaker labels are unreliable — read content, not just labels.
- Calls starting mid-call: score only observable sections, note as "partial."
- Exclude any manager/riding-partner or personal-phone segments embedded in a call — do
  not score these, and do not add commentary about their content. Grade only the
  customer-facing interaction.

## Step 4 — Score each rep (STRENGTH BANDS — never numbers)
For each Service tech with at least one genuine customer call, aggregate across all their
TARGET-day calls and produce ONE HTML coaching plan.

Rate every section on the **Strength Scale (words only)** — never a number, never "X/5",
never points, never a grade %:
- **Strong** = consistently good across the rep's calls
- **Strong on wins** = good on closed calls, drops on no-closes
- **Solid** = reliably present, not a standout
- **Moderate** = inconsistent
- **Weak** = rarely or poorly done

Rate the four SERVICE sections (bands must mirror "LIVE COACH/scoring_prompt_service.txt"
exactly — do not invent different section names or definitions):
- **Welcome** — greeting, professionalism, setting expectations for the visit (what will
  happen, roughly how long, what it will cost to look).
- **Diagnosis** — thorough assessment: actually inspecting/testing the system, explaining
  findings in plain language, checking in with the customer during the work.
- **Options** — building and presenting good/better/best repair-and-upgrade options
  (target 4-6 real options) with clear, specific pricing for each.
- **Close** — asking for the business directly, handling objections with empathy plus a
  question (not folding), confirming next steps or scheduling the work.

4 Critical Actions (Pass/Fail), same as the live-scoring rubric:
- **expectations** — clear expectations set at the start of the visit (cost to diagnose,
  what happens next).
- **options** — 4 or more distinct repair/upgrade options presented.
- **membership** — the membership / SAM maintenance agreement offered at some point.
- **close** — a direct ask for the sale (not just "let me know").

**Number rule — read carefully:** The ONLY number allowed anywhere in the report is the
**close-rate percentage** = closed calls ÷ gradeable calls for that tech on TARGET, e.g.
"44%". Do NOT output a total score, a /170, a points value, an A–F grade, or a grade %.
Every category is a word band. NEVER mention TGLs, flips, or Option C — those are SILO
concepts that do not apply to Service.

**Outcome:** "closed" (customer approved the work / bought the repair or upgrade on this
call), "no-close" (customer declined or deferred), or "unknown" (call ended before a
decision). Card-backed calls already carry this outcome from live scoring; for fallback
calls, judge outcome from the transcript itself — there is no ServiceTitan TGL
cross-reference for Service calls (unlike the SILO tgl_truth rule), so transcript content
IS the ground truth here.

**Every band must be backed by a real quote** from that rep's transcripts — the Evidence
quotes inside the scorecards ARE transcript quotes and satisfy this rule. Any missed
Critical Action = automatic FAIL on that call regardless of bands. Flag it prominently.
For FALLBACK (uncarded) calls: read the ENTIRE transcript — do not truncate or summarize
early. Every quote used in the plan must trace to transcript text (directly, or via a
card's Evidence line).

## Step 5 — Generate HTML coaching plans
Use REPO/coaching/genplan.py in SERVICE mode (spec has `"dept": "service"` at the top
level — see genplan.py's service section-name/band mapping) so the exact
EXEMPLAR_Benjamin_Wyllie.html <style> block and class structure is reused, with Service
section names substituted for the SILO ones. Every plan must include:
- Navy header with stat tiles: Close Rate (the only number), Calls Reviewed, Strongest
  Section, Weakest Section — NO total score, NO grade
- "Strength by Section" — a word band (Strong / Strong on wins / Solid / Moderate / Weak)
  per section (Welcome / Diagnosis / Options / Close), shown as a labeled band pill (no
  numbers on the bars)
- "Critical Actions — Pass Rate" badges (green = pass, red = fail/flagged) for
  expectations / options / membership / close
- strength cards (genuine strengths with the rep's actual quotes)
- Gap sections (Gap 1, Gap 2, Gap 3) with actual quotes showing the gap
- 3-week training plan rows
- "What We Owe [Rep Name]" commitment section
- Navy "Bottom Line" closing
Lead with genuine strengths. Use the rep's real words/quotes. Be specific — not "could
build more value" but "When [customer] asked about the noise, [Tech] answered with X —
here's what the model calls for instead."
Save each plan to: plans_service/TARGET/[Rep Name].html (underscores in the filename,
e.g. Fabian_Pantoja.html)

## Step 6 — Generate _index.html
Create _index.html in plans_service/TARGET/ showing: the TARGET date, all techs scored
(close rate %, strongest section band, weakest section band, headline gap — NO total
score, NO grade), and the list of skipped recordings with reasons.

## Step 6b — Emit service_coaching.json (dashboard feed) at the REPO ROOT
Write `service_coaching.json` at the repository root (overwrite if it exists) — NEVER
write to coaching.json, that file belongs to the SILO routine only.

**Newest-day invariant (2026-08-06) — the top-level `date` and `reps` must always describe
the NEWEST day present in plans_service/, which is not always TARGET.** When TARGET is a
catch-up day older than a day already published, build `date`/`reps` from that newest
published day and merely fold TARGET into `dates`/`history`. Never let a catch-up run move
`date` backwards. Backfilling 2026-08-04 after 2026-08-05 was already live rewrote this
file's `date` to 08-04 and the dashboard's Service coaching view regressed a full day — the
plan HTMLs were all correct, the summary just pointed at the wrong one, and health.json
then reported "Service daily plans 2 days behind" against complete data.
```
{
  "generated": "<ISO timestamp with offset, America/Los_Angeles>",
  "date": "<TARGET date YYYY-MM-DD>",
  "reps": [
    {
      "name": "Rep Name",
      "closeRate": "44%",
      "calls": 3,
      "strongest": "Welcome",
      "weakest": "Options",
      "bands": {"welcome": "Solid", "diagnosis": "Moderate", "options": "Weak", "close": "Strong"},
      "critical": {"expectations": true, "options": false, "membership": true, "close": true},
      "headlineGap": "one-sentence top gap — NO customer names, NO quotes",
      "focus": ["Week 1: ...", "Week 2: ...", "Week 3: ..."],
      "plan": "coaching/plans_service/<TARGET>/Rep_Name.html"
    }
  ],
  "skipped": [{"rep": "Name or Unknown", "reason": "one line"}],
  "dates": ["<TARGET>", "...older dates that exist under plans_service/, newest first, max 14"],
  "history": {
    "Rep Name": [
      {"date": "<YYYY-MM-DD>", "plan": "coaching/plans_service/<YYYY-MM-DD>/Rep_Name.html"}
    ]
  }
}
```
Rules:
- Band values must be ONLY the five words Strong / Strong on wins / Solid / Moderate /
  Weak — never numbers.
- `closeRate` is the ONLY number-bearing field (a percent string, e.g. "44%") = closed
  calls ÷ gradeable calls for that tech on TARGET, rounded to the nearest whole percent.
- `critical` values are booleans — true means the rep passed that critical action on ALL
  of their gradeable calls that day.
- `headlineGap` and each `focus` entry must contain NO customer names and NO transcript
  quotes, and must NEVER mention TGLs/flips/Option C.
- `plan` is the dashboard-relative path where the relay workflow publishes the HTML:
  `coaching/plans_service/<TARGET>/<Rep_Name with underscores>.html`.
- `dates` lists the dated folders under `plans_service/` newest-first, max 14 entries.
- `history` is built MECHANICALLY from the filesystem: for EVERY dated folder under
  `plans_service/` (all of them, not just the last 14), list each `*.html` file except
  `_index.html`; the rep name is the filename with underscores converted to spaces and
  `.html` dropped; group entries per rep, newest date first.

## Step 7 — Report
Print a summary: TARGET date processed (and why it was chosen), techs scored with their
close rates, recordings skipped, files saved. Confirm no file under plans/, coaching.json,
weekly/, monthly/, or objections/ was touched by this run.

## SERVICE SCOPE RULE (locked — John, 2026-08-04)
Service techs are NOT scored on flipping, TGL creation, or Option C — that is exclusively
a SILO concept and must never appear in a Service plan, gap, or coaching tip. Service
coaching is scoped to: setting expectations, diagnosis quality, building/presenting
good-better-best repair options with pricing, offering the membership, and directly
asking for the sale. Never band a Service tech down for anything outside those four
sections and the four critical actions above.
