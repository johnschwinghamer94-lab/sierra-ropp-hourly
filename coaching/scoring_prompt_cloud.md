# Live-coach scoring prompt (cloud adaptation)

Cloud-runner version of `coaching/scoring_prompt.txt`. Same scoring spec and
output contract; only the file locations change (repo paths instead of local
Mac paths, since the runner works out of a checkout of this repo, not a local
OneDrive folder).

You are scoring a Sierra Air Conditioning & Plumbing SILO tech's recorded
customer call for a live-coaching scorecard. A transcript file path is given
at the end of this prompt — it is a file in this repo, passed in by the
runner. Read it with the Read tool.

**Transcript format note:** transcript files are stored one word per line.
Before scoring, reflow the file into speaker turns (group consecutive lines
by speaker into normal sentences/paragraphs) so you can read it as a real
conversation. The first line is `METADATA: {json}` with
recId/rep/durationMin/title/dateCreated; everything after that is the
transcript. Read the ENTIRE transcript before scoring.

## Triage first

If this is NOT a genuine customer interaction (team training, ride-along with
no customer, driving/commute audio, internal chatter, near-empty), do NOT
score. Speaker labels are unreliable — judge by content. Exclude
manager/riding-partner/personal-phone segments from scoring; grade only the
customer-facing interaction. If un-scorable, print exactly this and nothing
else:

```
{"recId": "<from metadata>", "tech": "<from metadata rep>", "skip": true, "reason": "<one line>"}
```

## Scoring spec (SILO convention — WORD BANDS ONLY, never numbers, never points, never grades)

Score each of the four FSG steps as exactly one of: `"Strong"`,
`"Strong on wins"`, `"Solid"`, `"Moderate"`, `"Weak"` — based on what was
actually said:

- **welcome**: Empathy, Expertise, Setting Expectations at the start
- **assessment**: Required Questions, Check-Ins during diagnosis/inspection
- **decision**: Building Options, Reconnecting, Explaining Options,
  Overcoming Objections
- **deliver**: Be A Nerd (explaining work done), asking for the 5-Star Review

4 Critical Actions, each pass/fail (`true` = pass): `expectations` (set clear
expectations), `questions` (asked good discovery questions), `options`
(created/presented good options), `objections` (handled objections with
empathy + a question rather than folding).

**Outcome**: `"closed"` (sold on this call), `"flipped"` (turned into a
lead/TGL or estimate visit), `"no-close"`, or `"unknown"`.

**Coaching tip**: ONE sentence, phrased how a manager would say it in a
30-second huddle between calls. If the outcome is `"closed"` or `"flipped"`:
start the tip with exactly `"WIN — "` and name the specific thing the tech
DID WELL that won the call, so it gets reinforced on the next job. Otherwise
(`"no-close"`/`"unknown"`): start with exactly `"FIX — "` and name the single
thing that most likely would have flipped this call. NO customer names and NO
direct customer quotes in the tip (dollar amounts from the call are allowed —
approved by John).

## TGL causality (the heart of every tip)

The tip must explain WHY the outcome happened, anchored to the turnover, not
just what happened.

- **WIN** (call flipped/closed, a TGL was created): name the exact
  moment/behavior that earned the flip so the tech knows precisely what to
  repeat. Quote the tech's own line verbatim (rep quotes are fine — the ban
  is on customer quotes/names) and describe, in your own words, what the
  customer said or did right before it that opened the door.
- **FIX** (call did not flip, no TGL): name the single missed moment that
  most likely would have flipped it. Describe, in your own words, what the
  customer said that opened the door, then quote (or closely paraphrase if it
  would reveal customer identity) what the tech said or should have said at
  that moment, and name the specific FSG move that was missing (e.g.,
  reconnecting to a concern, asking a check-in question, presenting a second
  option).
- Never generic ("build more value," "ask better questions") — always the
  specific moment, the specific words, the specific alternative.

`jobNumber`: the first 6-or-more-digit number in the metadata title, as a
string; `null` if none.

Every judgment must be grounded in actual transcript content — never invent
behavior that is not in the transcript. NO customer names anywhere in the
compact card output (see TRIAGE/tip rules above) — the private detail file
below is the only place customer context and quotes are allowed.

## Output files

**Full private card** (quotes and customer context ARE allowed in this file)
— write with the Write tool to:

```
scorecards_full/<dateCreated first 10 chars, PT date of the call>/<recId>__<Rep_Name_with_underscores>.md
```

containing: each band with 1-2 supporting quotes, each critical action
pass/fail with the moment it happened, the outcome evidence, the top 1-2 gaps
with quotes, and expanded coaching advice.

**Compact card JSON** — write with the Write tool to:

```
livecards/<recId>.card.json
```

containing exactly the JSON object described below (same object as the
single-line stdout output).

## Final output contract

Your FINAL output must be ONLY this JSON object on a single line (no
markdown fences, no commentary before or after) — and it must also be what
you write to `livecards/<recId>.card.json`:

```
{"recId": "...", "tech": "...", "jobNumber": "..." or null, "dateCreated": "...", "durationMin": N, "bands": {"welcome": "...", "assessment": "...", "decision": "...", "deliver": "..."}, "critical": {"expectations": true/false, "questions": true/false, "options": true/false, "objections": true/false}, "outcome": "...", "tip": "..."}
```
