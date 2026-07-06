#!/usr/bin/env python3
"""
graph_hourly.py — FULLY-FREE cloud capture, now reading straight from EMAIL.

Runs in GitHub Actions on a schedule. Pulls the newest hourly "today-only" ServiceTitan
exports directly from the Outlook "Service Titan Reports" folder via Microsoft Graph
(delegated Mail.Read) — no OneDrive, no Power Automate. Counts today's distinct
ROPPs/TGLs the same way the dashboard engine does, and publishes the SHARED
hourly_state.json + hourly.json to the PUBLIC dashboard repo. Rotates its own Graph
refresh-token secret so it never expires.

Secrets (this private repo -> Settings -> Secrets -> Actions):
  GRAPH_CLIENT_ID, GRAPH_TENANT_ID, GRAPH_REFRESH_TOKEN  (refresh token must be
  consented for Mail.Read + offline_access), DASHBOARD_TOKEN.
"""
import os, io, json, base64
from datetime import datetime
import requests
try:
    from zoneinfo import ZoneInfo
    def _now(): return datetime.now(ZoneInfo("America/Los_Angeles"))
except Exception:
    def _now(): return datetime.utcnow()

CLIENT = os.environ["GRAPH_CLIENT_ID"]
TENANT = os.environ["GRAPH_TENANT_ID"]
RTOKEN = os.environ["GRAPH_REFRESH_TOKEN"]
GHTOK  = os.environ["DASHBOARD_TOKEN"]
SELF   = os.environ.get("GITHUB_REPOSITORY", "johnschwinghamer94-lab/sierra-ropp-hourly")
PUB    = "johnschwinghamer94-lab/sierra-ropp-dashboard"
FOLDER = "Service Titan Reports"
GRAPH  = "https://graph.microsoft.com/v1.0"

GH = {"Authorization": "token " + GHTOK, "Accept": "application/vnd.github+json"}
PUBAPI = "https://api.github.com/repos/" + PUB + "/contents/"


# ---------- Microsoft Graph (delegated, refresh-token flow) ----------
def graph_token():
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        data={"client_id": CLIENT, "grant_type": "refresh_token",
              "refresh_token": RTOKEN, "scope": "Mail.Read offline_access"})
    r.raise_for_status()
    j = r.json()
    return j["access_token"], j.get("refresh_token")


def rotate_secret(new_rt):
    if not new_rt or new_rt == RTOKEN:
        return
    try:
        from nacl import public, encoding
        pk = requests.get(f"https://api.github.com/repos/{SELF}/actions/secrets/public-key", headers=GH).json()
        sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder)).encrypt(new_rt.encode())
        requests.put(
            f"https://api.github.com/repos/{SELF}/actions/secrets/GRAPH_REFRESH_TOKEN",
            headers=GH,
            json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]}
        ).raise_for_status()
        print("Rotated GRAPH_REFRESH_TOKEN.")
    except Exception as e:
        print("WARN: could not rotate refresh token:", e)


def _get(tok, url):
    return requests.get(url, headers={"Authorization": "Bearer " + tok}).json()


def folder_id(tok):
    from urllib.parse import quote
    j = _get(tok, f"{GRAPH}/me/mailFolders?$filter=displayName eq '{quote(FOLDER)}'&$select=id,displayName")
    vals = j.get("value", [])
    if not vals:
        raise SystemExit(f"Mail folder '{FOLDER}' not found")
    return vals[0]["id"]


def newest_message(tok, fid, *subs):
    """id of the newest message in the folder whose subject contains all subs (lowercased)."""
    url = (f"{GRAPH}/me/mailFolders/{fid}/messages"
           "?$top=40&$orderby=receivedDateTime desc&$select=id,subject,receivedDateTime")
    for m in _get(tok, url).get("value", []):
        s = (m.get("subject") or "").lower()
        if all(sub in s for sub in subs):
            return m["id"]
    return None


def rows_from_message(tok, msg_id):
    """Load the .xlsx file attachment of a message into rows (via Graph contentBytes)."""
    from openpyxl import load_workbook
    j = _get(tok, f"{GRAPH}/me/messages/{msg_id}/attachments?$select=name,contentType,contentBytes")
    for a in j.get("value", []):
        name = (a.get("name") or "").lower()
        if name.endswith(".xlsx") and a.get("contentBytes"):
            data = base64.b64decode(a["contentBytes"])
            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            return [list(r) for r in wb.active.iter_rows(values_only=True)]
    return None


# ---------- count exactly like UPDATE_DASHBOARD.iter_grouped ----------
def _grouped(rows, jobcol):
    for r in rows[1:]:
        a = r[0] if r else None
        if isinstance(a, str) and a.strip().startswith("Assigned Technicians:"):
            continue
        if len(r) <= jobcol or r[jobcol] is None:
            continue
        jb = r[jobcol]
        s = str(int(jb)) if isinstance(jb, (int, float)) and float(jb).is_integer() else str(jb).strip()
        if not s.isdigit() or len(s) < 6:
            continue
        yield r


def count(rev_rows, tgl_rows):
    calls = sum(1 for _ in _grouped(rev_rows, 3))
    tgls  = sum(1 for _ in _grouped(tgl_rows, 1))
    return calls, tgls


def rate(a, b):
    return round(a / b * 1000) / 10 if b else 0.0


# ---------- publish shared state to the public repo ----------
def pget(path):
    r = requests.get(PUBAPI + path, headers=GH)
    if r.status_code == 200:
        j = r.json()
        return json.loads(base64.b64decode(j["content"])), j["sha"]
    return None, None


def pput(path, obj, sha, msg):
    body = {"message": msg, "content": base64.b64encode(json.dumps(obj).encode()).decode(), "branch": "main"}
    if sha:
        body["sha"] = sha
    requests.put(PUBAPI + path, headers=GH, json=body).raise_for_status()


def main():
    tok, new_rt = graph_token()
    rotate_secret(new_rt)

    fid = folder_id(tok)
    # hourly "today-only" exports have " - Copy" in the subject (the morning full-year
    # reports do NOT). The revenue subject also literally contains "copy" ("Johns Copy
    # of Ericka's..."), so match the trailing " - copy" to tell them apart.
    rev_id = newest_message(tok, fid, "revenue by job", " - copy")
    tgl_id = newest_message(tok, fid, "tgls created", " - copy")
    if not rev_id or not tgl_id:
        print("No hourly '- Copy' revenue/tgls email found yet; nothing to publish.")
        return
    rev_rows = rows_from_message(tok, rev_id)
    tgl_rows = rows_from_message(tok, tgl_id)
    if not rev_rows or not tgl_rows:
        print("Report email had no .xlsx attachment; nothing to publish.")
        return

    calls, tgls = count(rev_rows, tgl_rows)
    n = _now(); today = n.date().isoformat(); hh = f"{n.hour:02d}"

    st, _ = pget("hourly_state.json")
    if not st or st.get("date") != today:
        st = {"date": today, "hours": {}}
    st["hours"][hh] = {"calls": calls, "tgls": tgls}

    hrs = sorted(st["hours"]); series = []; pc = pt = 0
    for h in hrs:
        c = st["hours"][h]["calls"]; t = st["hours"][h]["tgls"]
        series.append({"hour": h, "calls": c, "tgls": t, "rate": rate(t, c),
                       "dcalls": max(c-pc, 0), "dtgls": max(t-pt, 0), "drate": rate(max(t-pt, 0), max(c-pc, 0))})
        pc, pt = c, t
    latest = series[-1] if series else {"calls": 0, "tgls": 0, "rate": 0}
    out = {"date": today, "updated": n.strftime("%I:%M %p").lstrip("0"),
           "today": {"calls": latest["calls"], "tgls": latest["tgls"], "rate": latest["rate"]},
           "hours": series}

    _, ssha = pget("hourly_state.json")
    pput("hourly_state.json", st, ssha, f"Cloud(email) hourly state {today} {hh}:00")
    _, osha = pget("hourly.json")
    pput("hourly.json", out, osha, f"Cloud(email) hourly capture {today} {hh}:00")
    print(f"Published {today} {hh}:00 -> {calls} calls / {tgls} TGLs ({rate(tgls, calls)}%)")


if __name__ == "__main__":
    main()
