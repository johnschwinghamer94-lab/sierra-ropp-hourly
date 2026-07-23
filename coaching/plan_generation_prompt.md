<!-- Copied from the Mac scheduled task ~/.claude/scheduled-tasks/silo-coaching-plans/SKILL.md on 2026-07-16; this repo copy is now the source of truth for the cloud plan-generation routine. Updated 2026-07-17: plans now build from scorecards_full cards (primary) with raw transcripts as fallback — token optimization. CLOUD PATH NOTES: transcripts live in transcripts/<date>/ in this repo; full scorecards will live in scorecards_full/<date>/ in this repo once the cloud scoring routine exists — until then ALL calls are "uncarded" and the transcript fallback applies to everything. Rubric: coaching/FSG-Grading-Rubric.md; exemplar: coaching/EXEMPLAR_Benjamin_Wyllie.html; output: plans/<date>/. -->


You are generating daily FSG coaching plans for Sierra Air Conditioning & Plumbing's Silo Techs team. This runs every morning at 7 AM, AFTER the 6 AM launchd script has pulled the prior day's transcripts.

## Paths (base folder = BASE)
BASE = /Users/johnschwinghamer/Library/CloudStorage/OneDrive-SierraCoolsLV/CLAUDE STUFF
- Rubric: BASE/RUBRIC TRAININGS/FSG-Grading-Rubric.md
- HTML template: BASE/RUBRIC TRAININGS/Coaching Plans/EXEMPLAR_Benjamin_Wyllie.html
- Transcripts root: BASE/SILO TRANSCRIPTS/
- Output root: BASE/RUBRIC TRAININGS/Daily Rep Training Guides/

## Step 0 — Determine which day to process (DO NOT trust the environment date)
The date shown in your context can be stale, so compute it yourself with bash in Las Vegas local time (this is macOS — use -v, not -d):
  TZ=America/Los_Angeles date +%F          # "today" locally
  TZ=America/Los_Angeles date -v-1d +%F    # normal target = yesterday
Resolve the TARGET date as follows:
  1. Normal case: TARGET = yesterday (local). Use it only if that transcript folder exists AND contains at least one non-empty .txt (excluding _summary.txt).
  2. Catch-up case: otherwise, scan the transcript folders named YYYY-MM-DD under the Transcripts root, ignore any starting with "_" (those are one-off exports), and pick the MOST RECENT dated folder that (a) has real .txt transcripts and (b) does NOT already have a matching output folder under the Output root. Process that day.
  3. If no such folder exists (nothing new to grade), STOP and report "No unprocessed transcript day found — nothing to do." Do not create empty output.
State clearly which TARGET date you resolved and why. The output folder is named for the TARGET (transcript) date — NOT today's date: Output root/TARGET/.

## Step 1 — Load reference files
Read FSG-Grading-Rubric.md and EXEMPLAR_Benjamin_Wyllie.html in full. Copy the EXEMPLAR's exact <style> block and HTML structure for every plan you generate.

## Step 2 — Load scorecards FIRST (primary input), raw transcripts only as fallback
The live-coach pipeline already scored each call during the day and saved a full
detail card (bands per section, verbatim transcript quotes as Evidence, gaps,
critical-action pass/fails) to: BASE/LIVE COACH/scorecards_full/TARGET/*.md
(also check scorecards_full/TARGET+1/ — evening calls after ~5 PM file under the
next UTC date). These cards are the PRIMARY analysis input — do NOT re-read raw
transcripts for calls that have a card. Quotes in a card's Evidence lines are
verbatim from the transcript and may be used directly in the plan.

Fallback ONLY for uncarded calls: list the TARGET transcript folder
(Transcripts root/TARGET/, skip _summary.txt) and match each transcript
(rep + Job # / customer in the filename) against the cards (rep + Job # in the
card header). For transcripts with NO matching card, read the raw .txt (one word
per line — reflow into speaker turns). If a needed quote for a card-backed call
isn't in the card, you may open that one call's transcript — but only that one.

## Step 3 — Triage every recording
Card-backed calls were already screened during scoring — take their bands/outcome
as given. Apply this triage to the FALLBACK transcripts (uncarded calls) you read:
- SCORE: genuine in-home customer sales or maintenance calls where a rep is interacting with a homeowner/decision-maker.
- SKIP (list with one-line reason): team training sessions, ride-alongs with no customer, driving/commute recordings, internal chatter, empty/near-empty recordings, and calls under ~5 minutes with no real customer dialogue.
- Speaker labels are unreliable — read content, not just labels. "Customer:" turns may be a trainer, GPS, or radio. Never invent behavior not in the transcript.
- Calls starting mid-call: score only observable sections, note as "partial."
- IMPORTANT: Exclude any halftime/manager/riding-partner or personal phone segments embedded in a call — do not score these, and do not add commentary about their content. Grade only the customer-facing interaction.

## Step 4 — Score each rep (STRENGTH BANDS — never numbers)
For each rep with at least one genuine sales/maintenance call, aggregate across all their TARGET-day calls and produce ONE HTML coaching plan.

Rate every rubric category on the **Strength Scale (words only)** — never a number, never "X/5", never points, never a grade %:
- **Strong** = consistently good across the rep's calls
- **Strong on wins** = good on closed calls, drops on no-closes
- **Solid** = reliably present, not a standout
- **Moderate** = inconsistent
- **Weak** = rarely or poorly done

Rate each section and its behaviors:
- Welcome Step (Empathy, Expertise, Setting Expectations)
- Assessment Step (Required Questions, Check-Ins, How to Check-In)
- Decision Step (Building Options, Reconnecting, Explaining Options, Overcoming Objections)
- Deliver Step (Be A Nerd, 5-Star Review)
- Two Key Objectives
- 4 Critical Actions (Pass/Fail): Setting clear expectations, Asking good questions, Creating good options, Handling objections

**Number rule — read carefully:** The ONLY number allowed anywhere in the report is the **close-rate percentage** = (rep's closed/flipped calls ÷ their gradeable calls), e.g. "44%". Do NOT output a total score, a /170, a points value, an A–F grade, or a grade %. Every category is a word band.
**Every band must be backed by a real quote** from that rep's transcripts — the
Evidence quotes inside the scorecards ARE transcript quotes and satisfy this rule.
Any missed Critical Action = automatic FAIL on that call regardless of bands. Flag it prominently.
For FALLBACK (uncarded) calls: read the ENTIRE transcript — do not truncate or
summarize early. Every quote used in the plan must trace to transcript text
(directly, or via a card's Evidence line).

## Step 5 — Generate HTML coaching plans
Use the EXEMPLAR_Benjamin_Wyllie.html's exact <style> block and class structure. Every plan must include:
- Navy header with stat tiles: Close Rate (the only number), Calls Reviewed, Strongest Section, Weakest Section — NO total score, NO grade
- "Strength by Rubric Section" — a word band (Strong / Strong on wins / Solid / Moderate / Weak) per section, shown as a labeled band pill (no numbers on the bars)
- "Critical Actions — Pass Rate" badges (green = pass, red = fail/flagged)
- strength cards (genuine strengths with the rep's actual quotes)
- Gap sections (Gap 1, Gap 2, Gap 3) with actual quotes from the transcript showing the gap
- 3-week training plan rows
- "What We Owe [Rep Name]" commitment section
- Navy "Bottom Line" closing
Lead with genuine strengths. Use the rep's real words/quotes. Be specific — not "could improve on objection handling" but "When [customer] said 'let me think about it,' [Rep] responded with X — here's what the FSG model calls for instead."
Save each plan to: Output root/TARGET/[Rep Name].html (use underscores in the filename, e.g. Benjamin_Wyllie.html)

## Step 6 — Generate _index.html
Create _index.html in Output root/TARGET/ showing: the TARGET date, all reps scored (close rate %, strongest section band, weakest section band, headline gap — NO total score, NO grade), and the list of skipped recordings with reasons.

## Step 7 — Report
Print a summary: TARGET date processed (and why it was chosen), reps scored with their close rates, recordings skipped, files saved.