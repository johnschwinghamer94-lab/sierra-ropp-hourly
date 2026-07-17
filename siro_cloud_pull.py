#!/usr/bin/env python3
"""
siro_cloud_pull.py — cloud replacement for the Mac's browser-based Siro
transcript downloads. Runs in GitHub Actions (workflow_dispatch for now; cron
gets enabled after secrets are added and a dispatch run verifies).

Uses Siro's REST API (validated 2026-07-16):
  - mint a user-scoped token via functions.siro.ai oauth/apps/{clientId}/access-token
  - list recordings via api.siro.ai/v1/core/recordings (x-siro-auth-token header)
  - fetch each recording's utterances/speakerMap for the transcript body

Transcript filename/content format is copied EXACTLY from the Mac's
"SILO TRANSCRIPTS/siro_livecoach_poll.py" (build_transcript / safe / fname),
and the date-folder is dateCreated[:10] (UTC) — same as the Mac writer — so
the two pipelines agree on names during the transition and never duplicate a
file under two different names. (Switching to Pacific dates is a post-cutover
fix, not part of this build.)

Auth (env vars, with local-file fallback so this is testable on a Mac that
already has Siro credentials cached):
  SIRO_API_KEY        - org API key           (fallback: ~/.siro_api_key)
  SIRO_CLIENT_ID       \\
  SIRO_CLIENT_SECRET    > oauth app            (fallback: ~/.siro_oauth_app.json)
  SIRO_USER_ID         - token-bound user id   (fallback: LOCAL_TEST_USER_ID below,
                                                 valid for local testing only)

Graph upload (env vars, reusing the token-refresh pattern from
graph_download_cloud.py / graph_hourly.py):
  GRAPH_CLIENT_ID, GRAPH_TENANT_ID, GRAPH_REFRESH_TOKEN
  DASHBOARD_TOKEN (optional) - if present, rotates GRAPH_REFRESH_TOKEN back into
                               this repo's secrets the same way graph_hourly.py does

Usage:
  python3 siro_cloud_pull.py           # pull + upload to OneDrive via Graph
  python3 siro_cloud_pull.py --dry     # pull + build transcripts, but skip the
                                        # Graph upload and write to ./siro_dry_output/
                                        # instead (for local testing without Graph creds)

Any per-recording error is logged and skipped; a missing secret set prints a
clear message and exits 0 (so a pre-secrets cron/dispatch run doesn't fail loud).
"""
import os, re, sys, json, base64, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "siro_pull_state.json"
DRY_OUTPUT_DIR = HERE / "siro_dry_output"
# In-repo mirror of every pulled transcript, committed by the workflow so the
# claude.ai cloud routines (plan generation / scoring) can read transcripts
# straight from the repo without holding any credentials. Owner-approved
# private-repo storage (2026-07-16).
TRANSCRIPTS_DIR = HERE / "transcripts"

TEAM_ID = "Q42L8L"
TOKEN_URL_FMT = "https://functions.siro.ai/api-externalApi/v1/core/oauth/apps/{client_id}/access-token"
API_BASE = "https://api.siro.ai/v1/core"

LOOKBACK_DAYS = 3          # pull finished recordings from the last N days
STATE_PRUNE_DAYS = 14      # keep pulled-ids in state for N days, then drop
MIN_DURATION_MS = 300_000  # skip recordings under 5 minutes — same floor as both
                           # Mac writers (siro_livecoach_poll / siro_download_mac)

GRAPH_FOLDER = "CLAUDE STUFF/SILO TRANSCRIPTS"
SELF = os.environ.get("GITHUB_REPOSITORY", "johnschwinghamer94-lab/sierra-ropp-hourly")

# Cloudflare blocks the default python-urllib/requests UA on *.siro.ai (403 code
# 1010) — every request to functions.siro.ai / api.siro.ai needs a browser UA.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LOCAL_SIRO_API_KEY_FILE = Path.home() / ".siro_api_key"
LOCAL_SIRO_OAUTH_APP_FILE = Path.home() / ".siro_oauth_app.json"
# Token-bound userId for LOCAL TESTING ONLY (per validated Siro API notes,
# 2026-07-16). Production always uses the SIRO_USER_ID secret; this constant
# is only consulted when that secret is absent, e.g. running by hand on a Mac
# that already holds the local Siro credential files.
LOCAL_TEST_USER_ID = "RKnwDLk8seVkMrLIhDdnSOVsEu73"


# ── credentials ──────────────────────────────────────────────────────────────
def get_siro_creds():
    api_key = os.environ.get("SIRO_API_KEY")
    client_id = os.environ.get("SIRO_CLIENT_ID")
    client_secret = os.environ.get("SIRO_CLIENT_SECRET")
    user_id = os.environ.get("SIRO_USER_ID")

    if not api_key and LOCAL_SIRO_API_KEY_FILE.exists():
        try:
            api_key = LOCAL_SIRO_API_KEY_FILE.read_text().strip()
        except OSError:
            pass
    if (not client_id or not client_secret) and LOCAL_SIRO_OAUTH_APP_FILE.exists():
        try:
            app = json.loads(LOCAL_SIRO_OAUTH_APP_FILE.read_text())
            client_id = client_id or app.get("clientID")
            client_secret = client_secret or app.get("clientSecret")
        except (OSError, json.JSONDecodeError):
            pass
    if not user_id and LOCAL_SIRO_API_KEY_FILE.exists() and LOCAL_SIRO_OAUTH_APP_FILE.exists():
        # Only fall back to the local-test user id when we're clearly in a local
        # testing context (both other local cred files are present too).
        user_id = LOCAL_TEST_USER_ID

    return api_key, client_id, client_secret, user_id


def get_graph_creds():
    return (os.environ.get("GRAPH_CLIENT_ID"),
            os.environ.get("GRAPH_TENANT_ID"),
            os.environ.get("GRAPH_REFRESH_TOKEN"))


# ── siro api ─────────────────────────────────────────────────────────────────
def mint_token(api_key, client_id, client_secret, user_id):
    url = TOKEN_URL_FMT.format(client_id=client_id)
    r = requests.post(url, json={"clientSecret": client_secret, "userId": user_id, "scope": "read"},
                       headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j["accessToken"]


def list_recordings(token):
    """All recordings newer than LOOKBACK_DAYS, any result state (caller filters
    'in progress' out). Assumes the API returns newest-first, same as the
    internal API the Mac's poller relies on."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    results, cursor = [], None
    while True:
        url = f"{API_BASE}/recordings?teamId={TEAM_ID}&limit=50"
        if cursor:
            url += f"&cursor={cursor}"
        r = requests.get(url, headers={"x-siro-auth-token": token, "User-Agent": UA}, timeout=60)
        r.raise_for_status()
        data = r.json()
        recs, cursor = data.get("data", []), data.get("cursor")
        stop = False
        for rec in recs:
            if rec.get("dateCreated", "") >= cutoff:
                results.append(rec)
            else:
                stop = True
                break
        if stop or not cursor:
            break
    return results


def build_transcript(token, rec):
    """EXACT copy of the format from SILO TRANSCRIPTS/siro_livecoach_poll.py's
    build_transcript(), so Mac-written and cloud-written files are byte-identical
    for the same recording."""
    r = requests.get(f"{API_BASE}/recordings/{rec['id']}/utterances",
                      headers={"x-siro-auth-token": token, "User-Agent": UA}, timeout=60)
    r.raise_for_status()
    payload = r.json().get("data", {})
    utterances = payload.get("utterances", [])
    speaker_map = payload.get("speakerMap", {})
    if not utterances:
        return None       # not processed yet — retry next run
    rep_name = f"{rec.get('repFirstName','')} {rec.get('repLastName','')}".strip()
    lines = ["TRANSCRIPT", f"Rep:      {rep_name}",
             f"Date:     {rec.get('dateCreated','')[:10]}",
             f"Duration: {round((rec.get('durationInMilliseconds') or 0)/60000)} min",
             f"Job:      {rec.get('title','').strip()}", "=" * 60, ""]
    prev = None
    for utt in utterances:
        text = (utt.get("utterance") or "").strip()
        if not text:
            continue
        name = speaker_map.get(str(utt.get("speakerTag", "0")), f"Speaker {utt.get('speakerTag','0')}")
        if name != prev:
            lines.append(f"\n{name}:")
            prev = name
        lines.append(f"  {text}")
    return "\n".join(lines)


_safe = lambda s: re.sub(r'[<>:"/\\|?*\r\n]', '', s).strip()


def transcript_filename(rec):
    rep = f"{rec.get('repFirstName','')} {rec.get('repLastName','')}".strip() or "Unknown"
    title = (rec.get("title") or "").strip()
    return f"{_safe(rep)} - {_safe(title)}.txt"[:120]


# ── state ────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"done": {}}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_PRUNE_DAYS)).isoformat()
    state["done"] = {k: v for k, v in state["done"].items() if v >= cutoff}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.rename(STATE_FILE)


# ── microsoft graph ──────────────────────────────────────────────────────────
def graph_token(client_id, tenant_id, refresh_token):
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={"client_id": client_id, "grant_type": "refresh_token",
              "refresh_token": refresh_token, "scope": "Files.ReadWrite offline_access"}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j["access_token"], j.get("refresh_token")


def rotate_graph_secret(new_rt, old_rt):
    """Same rotation pattern as graph_hourly.py / graph_download_cloud.py — only
    runs if DASHBOARD_TOKEN is present (a GitHub PAT with secrets:write on this
    repo). Keeps the shared GRAPH_REFRESH_TOKEN secret current for every job that
    uses it, since Azure can roll the token on each redemption."""
    if not new_rt or new_rt == old_rt:
        return
    ghtok = os.environ.get("DASHBOARD_TOKEN")
    if not ghtok:
        print("NOTE: GRAPH_REFRESH_TOKEN rotated but DASHBOARD_TOKEN is not set — "
              "not persisting the new token back to repo secrets.")
        return
    try:
        from nacl import public, encoding
        gh = {"Authorization": "token " + ghtok, "Accept": "application/vnd.github+json"}
        pk = requests.get(f"https://api.github.com/repos/{SELF}/actions/secrets/public-key", headers=gh).json()
        sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder)).encrypt(new_rt.encode())
        requests.put(
            f"https://api.github.com/repos/{SELF}/actions/secrets/GRAPH_REFRESH_TOKEN", headers=gh,
            json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]}).raise_for_status()
        print("Rotated GRAPH_REFRESH_TOKEN.")
    except Exception as e:
        print("WARN: could not rotate refresh token:", e)


def graph_item_exists(tok, rel_path):
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(rel_path)}"
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    r.raise_for_status()


def graph_upload(tok, rel_path, content_bytes):
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(rel_path)}:/content"
    r = requests.put(url, headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/plain"},
                      data=content_bytes, timeout=60)
    r.raise_for_status()


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                     help="skip the Graph upload; write transcripts to ./siro_dry_output/ instead")
    args = ap.parse_args()

    api_key, client_id, client_secret, user_id = get_siro_creds()
    if not all([api_key, client_id, client_secret, user_id]):
        print("Missing Siro credentials (need SIRO_API_KEY, SIRO_CLIENT_ID, SIRO_CLIENT_SECRET, "
              "SIRO_USER_ID as env vars, or the local ~/.siro_api_key + ~/.siro_oauth_app.json "
              "files) — nothing to do this run.")
        return 0

    # Graph/OneDrive is OPTIONAL: the repo transcripts/ mirror is the primary
    # store (cloud routines read it). Files.ReadWrite needs Sierra IT admin
    # consent (requested 2026-07-17, pending) — until granted, the upload is
    # skipped and the pull still succeeds via the repo mirror.
    graph_client_id = graph_tenant_id = graph_refresh_token = None
    if not args.dry:
        graph_client_id, graph_tenant_id, graph_refresh_token = get_graph_creds()
        if not all([graph_client_id, graph_tenant_id, graph_refresh_token]):
            print("NOTE: Graph credentials absent — OneDrive upload disabled, repo mirror only.")
            graph_client_id = None

    # Secrets PRESENT but not working = a real failure that must show red in
    # Actions (a green no-op run masks a bad secret — learned 2026-07-17 #3).
    # Only the secrets-entirely-absent case above exits 0.
    try:
        token = mint_token(api_key, client_id, client_secret, user_id)
    except Exception as e:
        print(f"FAILED to mint Siro access token (check SIRO_* secrets): {e}")
        return 1

    try:
        recs = list_recordings(token)
    except Exception as e:
        print(f"FAILED to list Siro recordings: {e}")
        return 1

    state = load_state()
    # Ready = the recording has a settled duration ≥ the floor, exactly like the
    # Mac writers. Do NOT gate on rec["result"]: that's Siro's sales-outcome tag
    # (flipped/closed/unknown), which can land HOURS after the call — gating on
    # it left the repo mirror 2/26 transcripts behind the Mac intraday
    # (2026-07-17). Still-processing calls are caught by the no-utterances
    # PENDING retry below.
    pending = [r for r in recs
               if (r.get("durationInMilliseconds") or 0) >= MIN_DURATION_MS
               and r.get("id") not in state["done"]]
    print(f"{len(recs)} recording(s) in the last {LOOKBACK_DAYS} day(s), "
          f"{len(pending)} ready (≥5 min) & not yet pulled")

    gtok = None
    if not args.dry and graph_client_id:
        try:
            gtok, new_rt = graph_token(graph_client_id, graph_tenant_id, graph_refresh_token)
            rotate_graph_secret(new_rt, graph_refresh_token)
        except Exception as e:
            print(f"NOTE: no Microsoft Graph token (Files.ReadWrite admin consent pending?) — "
                  f"OneDrive upload disabled this run, repo mirror only. Detail: {e}")
            gtok = None
    elif args.dry:
        DRY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pulled = skipped_exists = errors = mirrored = 0
    for rec in pending:
        rid = rec.get("id")
        rep = f"{rec.get('repFirstName','')} {rec.get('repLastName','')}".strip() or "Unknown"
        title = (rec.get("title") or "").strip()
        try:
            text = build_transcript(token, rec)
            if text is None:
                print(f"  PENDING (no utterances yet): {rep} — {title[:50]}")
                continue
            date_str = rec.get("dateCreated", "")[:10]
            fname = transcript_filename(rec)
            content = text.encode("utf-8")

            if args.dry:
                folder = DRY_OUTPUT_DIR / date_str
                folder.mkdir(parents=True, exist_ok=True)
                (folder / fname).write_bytes(content)
                print(f"  DRY WROTE: {date_str}/{fname}")
                pulled += 1
            else:
                repo_file = TRANSCRIPTS_DIR / date_str / fname
                if not repo_file.exists():
                    repo_file.parent.mkdir(parents=True, exist_ok=True)
                    repo_file.write_bytes(content)
                    mirrored += 1
                    pulled += 1
                    print(f"  MIRRORED: transcripts/{date_str}/{fname}")
                if gtok:
                    rel_path = f"{GRAPH_FOLDER}/{date_str}/{fname}"
                    if graph_item_exists(gtok, rel_path):
                        print(f"  SKIP (already exists on OneDrive): {rel_path}")
                        skipped_exists += 1
                    else:
                        graph_upload(gtok, rel_path, content)
                        print(f"  UPLOADED: {rel_path}")
            state["done"][rid] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            print(f"  ERROR on recording {rid} ({rep} — {title[:50]}): {e}")
            errors += 1
            continue

    if not args.dry:
        save_state(state)
    print(f"Done — {pulled} pulled, {skipped_exists} skipped (already on OneDrive), "
          f"{mirrored} mirrored into transcripts/, {errors} error(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
