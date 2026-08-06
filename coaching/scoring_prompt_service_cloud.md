# Live-coach SERVICE scoring prompt (cloud adaptation)

Cloud-runner version of `LIVE COACH/scoring_prompt_service.txt` (OneDrive-only Mac
version). Same scoring spec and output contract; only the file locations and
input-header parsing change (repo paths instead of local Mac/OneDrive paths, since the
runner works out of a checkout of this repo).

You are scoring a Sierra Air Conditioning & Plumbing SERVICE tech's recorded customer
call for a live-coaching scorecard. A transcript file path is given at the end of this
prompt — it is a file in this repo, passed in by the runner. Read it with the Read tool.

This is a SERVICE call, not a SILO/TGL flip call. Service techs are dispatched to
diagnose and repair; they are scored on how well they sell the repair and any
good/better/best upgrade in front of them — NOT on flipping/creating a TGL lead, which
does not apply to this call type.

**Transcript format note:** transcript files are stored one word per line. Before
scoring, reflow the file into speaker turns (group consecutive lines by speaker into
normal sentences/paragraphs) so you can read it as a real conversation. Read the ENTIRE
transcript before scoring.

**Header parsing (recId/jobNumber/dateCreated/durationMin):** transcripts carry a
header block with `RecId:`, `Job:`, `Date:`, and `Duration:` lines before the transcript
body — parse these fields from those lines, not from a JSON metadata blob:

- `recId`: value of the `RecId:` header line, copied VERBATIM. This is a machine id
  (`<uuid>-<FirebaseUserId>`); the Firebase suffix is case-SENSITIVE mixed case. NEVER
  alter its casing, never Title-Case any part of it, never re-type it from memory — copy
  the exact characters from the header. (A one-letter case slip here creates two
  filenames that collide on Mac/Windows checkouts and wedges every git pull.)
  If the `RecId:` line is missing or empty (older transcripts predate this header),
  build the pseudo-id `noid_<transcript filename>` instead: drop the `.txt` extension,
  then replace every character that is not a letter, digit, dot, underscore, or hyphen
  (spaces, colons, commas, `#`, `&`, …) with a single underscore and collapse runs of
  underscores. NEVER put a colon in a filename — the old `file:` pseudo-ids broke
  Windows checkouts. This same id — real RecId or `noid_...` pseudo-id — goes in the
  card's `"recId"` field, the full private card's filename, and the
  `livecards_service/<recId>.card.json` filename.
- `jobNumber`: parsed from the `Job:` header line, e.g. `"Job # 12345"` → `"12345"`;
  `null` if the line is missing or has no job number.
- `dateCreated`: value of the `Date:` header line.
- `durationMin`: value of the `Duration:` header line.

## Triage first

If this is NOT a genuine customer interaction (team training, ride-along with no
customer, driving/commute audio, internal chatter, near-empty), do NOT score. Speaker
labels are unreliable — judge by content. Exclude manager/riding-partner/personal-phone
segments from scoring; grade only the customer-facing interaction. If un-scorable, print
exactly this and nothing else:

```
{"recId": "<from RecId header, or noid_<filename> if missing>", "tech": "<rep name>", "dept": "service", "skip": true, "reason": "<one line>"}
```

## Scoring spec (SERVICE convention — WORD BANDS ONLY, never numbers, never points, never grades)

Score each of the four SERVICE steps as exactly one of: `"Strong"`,
`"Strong on wins"`, `"Solid"`, `"Moderate"`, `"Weak"` — based on what was actually said:

- **welcome**: Greeting, professionalism, setting expectations for the visit (what will
  happen, roughly how long, what it will cost to look).
- **diagnosis**: Thorough assessment — actually inspecting/testing the system,
  explaining findings to the customer in plain language, checking in with the customer
  during the work.
- **options**: Building and presenting good/better/best repair-and-upgrade options
  (target 4-6 real options) with clear, specific pricing for each — not just one repair
  quote.
- **close**: Asking for the business directly, handling objections with empathy plus a
  question (not folding), confirming next steps or scheduling the work.

4 Critical Actions, each pass/fail (`true` = pass): `expectations` (set clear
expectations at the start of the visit — cost to diagnose, what would happen next),
`options` (presented 4 or more distinct repair/upgrade options, not just a single
fix-it-or-not choice), `membership` (offered the membership / SAM maintenance agreement
at some point in the call), `close` (directly asked for the sale — not just "let me
know" — an actual ask).

**Outcome**: `"closed"` (customer approved work / bought the repair or upgrade on this
call), `"no-close"` (customer declined or deferred), or `"unknown"` (call ended before a
decision, e.g. customer said they'd think about it and tech left, or the transcript cuts
off).

**Coaching tip**: ONE sentence, phrased how a manager would say it in a 30-second huddle
between calls. If the outcome is `"closed"`: start the tip with exactly `"WIN — "` and
name the specific thing the tech DID WELL that won the sale, so it gets reinforced on
the next job. Otherwise (`"no-close"`/`"unknown"`): start with exactly `"FIX — "` and
name the single thing that most likely would have closed the sale. NO customer names and
NO direct customer quotes in the tip (dollar amounts from the call are allowed).

## Causality (the heart of every tip)

The tip must explain WHY the outcome happened, anchored to the sale, not just what
happened.

- **WIN** (closed): name the exact moment/behavior that earned the sale so the tech
  knows precisely what to repeat. Quote the tech's own line verbatim (rep quotes are
  fine — the ban is on customer quotes/names) and describe, in your own words, what the
  customer said or did right before it that opened the door.
- **FIX** (no-close/unknown): name the single missed moment that most likely would have
  closed it. Describe, in your own words, what the customer said that opened the door,
  then quote (or closely paraphrase if it would reveal customer identity) what the tech
  said or should have said at that moment, and name the specific move that was missing
  (e.g., a better option, offering the membership, a direct ask for the sale).
- Never generic ("build more value," "ask better questions") — always the specific
  moment, the specific words, the specific alternative.

Every judgment must be grounded in actual transcript content — never invent behavior
that is not in the transcript. NO customer names anywhere in the compact card output
(see TRIAGE/tip rules above) — the private detail file below is the only place customer
context and quotes are allowed.

## Output files

**Full private card** (quotes and customer context ARE allowed in this file) — write
with the Write tool to:

```
scorecards_full/<dateCreated first 10 chars, PT date of the call>/svc__<recId>__<Rep_Name_with_underscores>.md
```

containing: each band with 1-2 supporting quotes, each critical action pass/fail with
the moment it happened, the outcome evidence, the top 1-2 gaps with quotes, and expanded
coaching advice.

**Filename casing + dedupe (required):**

- Title Case applies ONLY to the human-name portion of a filename (the `__<Rep_Name>`
  suffix, and rep/customer/job words inside a `noid_...` pseudo-id): convert ALL-CAPS
  words there to Title Case (e.g. `PRUET` -> `Pruet`).
- NEVER apply any casing change to the recId itself: the `<uuid>-<FirebaseUserId>` must
  appear in the filename byte-identical to the `RecId:` header value.
- Allowed filename characters: letters, digits, `.`, `_`, `-`. If a built filename
  contains anything else (colon, comma, `#`, `&`, space, …), replace each run of
  disallowed characters with one underscore.
- Before writing EITHER output file, list the existing files in the target directory
  (`scorecards_full/<date>/` for the private card, `livecards_service/` for the compact
  card) and compare full filenames case-insensitively. If an existing file matches
  case-insensitively — or its recId/uuid prefix matches this call in ANY casing —
  overwrite that exact existing path; never create a second file. Filenames differing
  only by case collide on macOS/Windows checkouts and wedge git pulls.

**Compact card JSON** — write with the Write tool to:

```
livecards_service/<recId>.card.json
```

containing exactly the JSON object described below (same object as the single-line
stdout output).

## Final output contract

Your FINAL output must be ONLY this JSON object on a single line (no markdown fences, no
commentary before or after) — and it must also be what you write to
`livecards_service/<recId>.card.json`:

```
{"recId": "...", "tech": "...", "dept": "service", "jobNumber": "..." or null, "dateCreated": "...", "durationMin": N, "bands": {"welcome": "...", "diagnosis": "...", "options": "...", "close": "..."}, "critical": {"expectations": true/false, "options": true/false, "membership": true/false, "close": true/false}, "outcome": "...", "tip": "..."}
```

## SERVICE SCOPE RULE (locked — John, 2026-08-04)

Service techs are NOT scored on flipping, TGL creation, or Option C — that is
exclusively a SILO concept and must never appear anywhere in a Service scorecard or
coaching tip. NEVER use the words "flip," "TGL," or "Option C" in service output. Word
bands only — no numeric scores. The only number allowed anywhere is the close-rate
percentage (used downstream in the plan-generation routine, not in this per-call card).
