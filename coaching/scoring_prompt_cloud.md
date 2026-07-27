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
conversation. Read the ENTIRE transcript before scoring.

**Header parsing (recId/jobNumber/dateCreated/durationMin):** newer
transcripts carry a header block with `RecId:`, `Job:`, `Date:`, and
`Duration:` lines before the transcript body — parse these fields from those
lines, not from a JSON metadata blob:

- `recId`: value of the `RecId:` header line (the Siro recording UUID). If
  that line is missing or empty (older transcripts predate this header), use
  the pseudo-id `"file:" + <transcript filename without .txt, spaces
  replaced with underscores>` instead. This same id — real RecId or the
  `file:...` pseudo-id — is what goes in the card's `"recId"` field, in the
  full private card's filename, and in the `livecards/<recId>.card.json`
  filename.
- `jobNumber`: parsed from the `Job:` header line, e.g. `"Job # 12345"` →
  `"12345"`; `null` if the line is missing or has no job number.
- `dateCreated`: value of the `Date:` header line.
- `durationMin`: value of the `Duration:` header line.

## Triage first

If this is NOT a genuine customer interaction (team training, ride-along with
no customer, driving/commute audio, internal chatter, near-empty), do NOT
score. Speaker labels are unreliable — judge by content. Exclude
manager/riding-partner/personal-phone segments from scoring; grade only the
customer-facing interaction. If un-scorable, print exactly this and nothing
else:

```
{"recId": "<from RecId header, or file:<filename> if missing>", "tech": "<rep name>", "skip": true, "reason": "<one line>"}
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

## OUTCOME GROUND TRUTH (ServiceTitan is authoritative)

John's conversion rates must reflect real TGL creation, not how a call
"sounded" — ST is authoritative:

- Outcome is `"flipped"` whenever the customer AGREES to Option C or a
  Comfort Advisor appointment is set/scheduled during the call — soft
  wording still counts if the appointment is set.
- BEFORE finalizing outcome, parse the call's job number from the
  transcript's `Job:` header and check `tgl_truth/<call PT date>.json`
  (also check the NEXT day's file — tickets are often typed late). If the
  job number appears as a key in either file's `tgls` object, outcome MUST
  be `"flipped"` regardless of how the conversation sounded.
- If the transcript clearly shows Option C agreement but no truth entry
  exists yet (ticket not typed yet), still record `"flipped"` and note
  "ticket pending" in the full private card.

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

## SILO PRICE RULE (locked — John, 2026-07-27)
The SILO team does NOT do price transparency — quoting or breaking down replacement
pricing is not part of their process. Therefore:
- NEVER band a tech down, list a gap, or lower a grade because they declined to give
  pricing details or deflected a price question.
- Redirecting a price question to the specialist / Option C path IS the correct
  process and should be credited as such (often the "great" move).
- Coaching output must never tell a SILO tech to be more transparent about price or
  to present pricing breakdowns.
