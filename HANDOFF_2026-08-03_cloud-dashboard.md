# Handoff — Cloud dashboard: what's built, what isn't, and what to ask the coworker

**Date:** 2026-08-03 · **Verified against:** `sierra-ropp-hourly` @ `03789dd`, `sierra-ropp-dashboard` @ `e67d2e3`

This note does two things:
1. Checks the five-piece plan against what is actually live right now.
2. Gives you your own answers to the five comparison questions, so you can put them
   side by side with your coworker's.

---

## Plain-English version of the plan

A version of the dashboard that lives on a free public website instead of only running
on one computer, so anyone with the link can check it from anywhere, and it updates
itself on a timer with no machine left running.

---

## Status at a glance

| # | Piece | Status | One-line reality |
|---|---|---|---|
| 1 | GitHub Pages hosting | ✅ **Built and live** | Free, already serving |
| 2 | Password screen | ❌ **Not built** | No gate exists anywhere in the site today |
| 3 | Auto-refreshing data | ✅ **Built, bigger than described** | 14 scheduled robots, no machine on |
| 4 | Ability to keep editing | ✅ **Built, two ways** | Push to `main`, or edit from the ⚙ panel in the page itself |
| 5 | Private repo, public site | ⚠️ **Not true today** | **Both** repos are public |

Two of the five need work. Detail below.

---

## 1. GitHub Pages — ✅ live

- Repo: `johnschwinghamer94-lab/sierra-ropp-dashboard`
- Live URL: `https://johnschwinghamer94-lab.github.io/sierra-ropp-dashboard/`
- Workflow: `.github/workflows/deploy.yml` — fires on **every push to `main`**, plus a
  manual button.
- It retries the GitHub Pages deploy up to **4 times** (20s / 40s / 60s waits) because
  GitHub's deploy step intermittently returns "Deployment failed, try again later."
  Only the 4th attempt is allowed to fail loudly.
- Deploys **queue** rather than cancel each other (`cancel-in-progress: false`). This was
  changed on 2026-07-24 — with the data relays pushing every 1–2 minutes, cancelling
  starved deploys entirely and the site lagged 20+ minutes on busy mornings.
- Cost: **$0.** Nothing here is on a paid plan.

Nothing to do on this piece.

---

## 2. Password screen — ❌ does not exist yet

Searched every page in the live repo — `index.html`, `test.html`, `test2.html`,
`service.html`, `index_test.html` — for a passcode prompt, a password input, or any
stored "unlocked" flag. **There is none.** Anyone with the URL sees the full dashboard
immediately.

The one credential prompt that *does* exist in `index.html` (around line 357) is not a
viewing gate — it's the ⚙ MANAGE panel asking for a **GitHub token so you can publish
edits**. It protects writing, not reading.

**Why this matters more than it sounds.** The public site is not just totals. The
published data files carry named detail:

- `livefeed.json` → job records with **customer name** (`"customer": "PHILLIPS ROBERT"`),
  job number, technician name, job type, dollar amounts, and timestamps.
- `scorecards.json` / `servicecards.json` → **per-employee performance grading** by name,
  with written critiques.
- `coaching.json` → coaching plans per rep.

So the link currently exposes customer names and named employee evaluations to anyone who
has it or guesses it.

**Set expectations honestly on what a password can do here.** On a static site the whole
page and its data files are downloaded by the browser *before* any JavaScript check runs.
A password screen is a **soft lock** — it stops a coworker who wanders onto the link, and
nothing more. Anyone who opens the browser's dev tools, or runs `curl` against the same
URL, gets the full contents regardless of the password. It is a curtain, not a lock. If
the customer names and employee scorecards need real protection, the fix is to stop
publishing them to a public URL — not to put a curtain in front of them.

That distinction is exactly what question 4 below is probing for.

---

## 3. Auto-refreshing data — ✅ built, and larger than the plan described

Fourteen scheduled workflows in this repo. Nothing runs on your computer; all of it runs
on GitHub's machines.

| Workflow | When it runs |
|---|---|
| `livefeed.yml` | Every ~15 min, 5:30 AM – 11 PM PT |
| `servicefeed.yml` | Every ~15 min, 5:30 AM – 11 PM PT |
| `servicedata.yml` | Every 15 min, 7 AM – 11 PM PT + full rebuild 5:35 AM PT |
| `graph_hourly.yml` | Every 15 min, overnight/daytime window |
| `siro_pull.yml` | Twice an hour (:07 and :37), most of the day |
| `staging.yml` | Hourly at :40 |
| `daily.yml` | Hourly overnight backup rebuild |
| `servicedeck.yml` | 5:40 AM PT and 1:10 PM PT |
| `bonus_backfill.yml` | Daily 9 PM PT (trailing 15 days) |
| `weekly_report.yml` | Mondays 7 AM PT |
| `weekreview.yml` | Sunday night prelim + Monday 6 AM PT final |
| `hourly.yml` | On push to `hourly_reports/` (Power Automate drops the file) |
| `coaching_relay.yml` | On push to `plans/`, `coaching.json`, `weekly/`, `monthly/` |
| `scorecards_relay.yml` | On push to `livecards/` |

Each one logs into ServiceTitan (or Siro / Microsoft Graph), builds fresh numbers, and
writes the result into the public dashboard repo — which triggers the Pages deploy in
piece 1. Free tier: GitHub Actions minutes are unlimited for public repos, so today this
costs nothing. **Note:** if you make `sierra-ropp-hourly` private (see piece 5), Actions
minutes stop being free and start drawing on the free monthly allowance — 2,000 min/month
on a free account. At this schedule density that allowance is likely to be tight, so
price it before flipping the switch.

---

## 4. Ability to keep editing — ✅ works, two different routes

**Route A — push a change.** Commit to `main` in `sierra-ropp-dashboard`; `deploy.yml`
picks it up and the site is live in roughly 1–2 minutes.

**Route B — edit from inside the live page.** The ⚙ MANAGE panel in `index.html` can
rewrite the live `index.html` through the GitHub API. It asks for a GitHub token, stores
it in **that browser's `localStorage`**, and PUTs the updated file straight to `main`.

Route B is convenient and is also a real key sitting in a browser. Worth knowing where
those tokens are: on whichever machines/browsers have used the panel. `forgetToken()` in
the panel clears one.

---

## 5. Private repo, public site — ⚠️ this is not the current state

**Both repositories are public.** Verified against the GitHub API today:

| Repo | Claimed | Actual |
|---|---|---|
| `sierra-ropp-dashboard` | public | **public** ✅ as intended |
| `sierra-ropp-hourly` | "PRIVATE" (README line 1) | **public** ❌ |

`README.md` in this repo opens with `# sierra-ropp-hourly (PRIVATE)` and states that raw
ServiceTitan reports "stay in this **private** repo and never touch the public dashboard."
That is the design intent, and it is a good design — but the repo setting does not match
it. The engine repo is browsable by anyone.

What is sitting in the open there right now:

- `scorecards_full/` — **536 files**, coaching write-ups with customer names and job
  numbers in the filenames
- `transcripts/` — **215 folders** of call transcripts
- `siro_pull_state.json` — 512 KB of pull state
- `coaching.json`, `plans/`, `weekly/`, `monthly/`, `objections/`

**The good news, and it is genuinely good:** no credentials are exposed by this. Scanned
both repos for committed tokens and keys (`ghp_`, `github_pat_`, inline `client_secret`)
— **clean, nothing found.** Every secret lives in GitHub Actions Secrets, which stay
encrypted and unreadable even on a public repo, are never printed in logs, and are not
handed to forks. The credential hygiene here is correct. The exposure is **data**, not
keys.

**One constraint to know before flipping the switch:** GitHub Pages from a *private* repo
requires a paid plan (Pro/Team). On the free tier, the repo that serves the site must be
public. That does not break the plan — the two-repo split is already the right shape:

- `sierra-ropp-hourly` (engine + raw data) → **should be private**, serves no site
- `sierra-ropp-dashboard` (published aggregates) → **stays public**, serves the site

Making the engine repo private is the highest-value single change on this list, and it
doesn't cost anything or require a plan upgrade.

---

## The five questions — your answers, ready to compare

Bring this table. Fill his column in as he answers.

| # | Question | **Your setup** | His setup |
|---|---|---|---|
| 1 | Where is it hosted? | GitHub Pages, free tier, $0/month. Auto-deploys on every push to `main` with 4 retry attempts. | |
| 2 | Does it refresh itself, or does a machine have to stay on? | Fully self-refreshing. 14 GitHub Actions workflows on timers, some as often as every 15 minutes. Nothing runs on your computer. One input still arrives via Power Automate dropping report files in. | |
| 3 | Repo public or private? | Both public today. The engine repo is *supposed* to be private and isn't — that's the open item. | |
| 4 | Password gate? How strong is it meant to be? | None today. Planned as a soft lock only. Be blunt on this one: on a static site any gate is a curtain — `curl` and dev tools walk right past it. | |
| 5 | **Whose credentials, and where are they stored?** | See below — the one that actually matters. | |

### Question 5 in detail — your side

Ten distinct secrets, all stored in **GitHub Actions Secrets** on `sierra-ropp-hourly`
(encrypted at rest, never printed, injected as environment variables at run time only):

| Secret | What it opens | Whose account |
|---|---|---|
| `ST_CREDS_JSON` | ServiceTitan API — OAuth2 client-credentials, tenant `SIE` | **Sierra Air's ServiceTitan tenant** |
| `DASHBOARD_TOKEN` | Write access to the public dashboard repo | A GitHub token under **your** account |
| `SIRO_CLIENT_ID` / `SIRO_CLIENT_SECRET` / `SIRO_API_KEY` / `SIRO_USER_ID` | Siro call-recording platform | Company Siro account |
| `GRAPH_CLIENT_ID` / `GRAPH_TENANT_ID` / `GRAPH_REFRESH_TOKEN` | Microsoft Graph — reads report exports out of OneDrive | **Delegated — acts as you**, your OneDrive |
| `SHEET_WEBHOOK` | Writes to the bonus sheet | Company sheet |

Two things to say plainly when you compare notes:

- The ServiceTitan credential is a **company API application against the company's live
  tenant**, not a personal read-only export. It can pull anything that app is scoped for.
- The Microsoft Graph credential is a **delegated refresh token** — the robot is
  effectively logged in as you. If you left the company or your password reset, that
  token dies and those workflows stop. If someone else's copy of this setup uses *their*
  delegated token, the same is true of them.

Ask your coworker the same two things: is his ServiceTitan access a company API app or a
personal login, and is anything in his setup running as *him* personally. That's the
difference most likely to matter — a dashboard the company depends on that quietly runs
on one person's individual credentials is a single point of failure, and it's also the
kind of thing IT will want to know about before it becomes a surprise.

Also worth asking where his secrets sit. GitHub Actions Secrets (yours) is a reasonable
answer. Pasted into a script, saved in a shared drive, or sitting in a `.env` on someone's
desktop is not — and if that's what he's got, that's a finding worth raising kindly.

---

## Recommended order of work

1. **Flip `sierra-ropp-hourly` to private.** Settings → General → Danger Zone → Change
   visibility. Nothing breaks: the workflows use `DASHBOARD_TOKEN` for cross-repo writes,
   which already works private. Price the Actions minutes first (see piece 3).
2. **Decide what the public site is allowed to publish.** Customer names, job numbers, and
   named employee scorecards are on a public URL with no gate right now. This is a policy
   call, not a technical one, and it's above the password question — a password doesn't
   fix it.
3. **Add the password screen** once 2 is settled, understanding it's a soft deterrent.
4. **Rotate the GitHub tokens** flagged as still-open in `HANDOFF_2026-07-08.md`.
5. **Fix the README** — it currently describes this repo as private, which it isn't. A doc
   that misstates a security property is worse than no doc.

---

## Open items carried forward from `HANDOFF_2026-07-08.md`

- Rotate the GitHub tokens shared in earlier chats. (Still open.)
