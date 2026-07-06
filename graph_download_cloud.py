#!/usr/bin/env python3
"""
graph_download_cloud.py — runs in GitHub Actions (private repo) for the DAILY
rebuild. Downloads the 4 FULL-YEAR ServiceTitan reports from OneDrive via Microsoft
Graph (delegated auth) into ./SILO_Reports/ under their canonical names, so the
engine (UPDATE_DASHBOARD.resolve_report) picks each one unambiguously. No machine,
no Power Automate premium. Auto-rotates the shared GRAPH_REFRESH_TOKEN secret.
"""
import os, re, base64, requests
from urllib.parse import quote

CLIENT = os.environ["GRAPH_CLIENT_ID"]
TENANT = os.environ["GRAPH_TENANT_ID"]
RTOKEN = os.environ["GRAPH_REFRESH_TOKEN"]
GHTOK  = os.environ["DASHBOARD_TOKEN"]
SELF   = os.environ.get("GITHUB_REPOSITORY", "johnschwinghamer94-lab/sierra-ropp-hourly")
FOLDER = "CLAUDE STUFF/SILO_Reports"
GH = {"Authorization": "token " + GHTOK, "Accept": "application/vnd.github+json"}

# canonical local name  ->  substring that identifies the report in OneDrive
PHRASES = {
    "Revenue_By_JobType.xlsx":  "revenue by job",
    "ROPP_TGLs_Created.xlsx":   "tgls created",
    "ROPP_TGLs_Scheduled.xlsx": "tgls scheduled",
    "ROPP_Cancelations.xlsx":   "cancelations",
}


def graph_token():
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
        data={"client_id": CLIENT, "grant_type": "refresh_token",
              "refresh_token": RTOKEN, "scope": "Files.Read offline_access"})
    r.raise_for_status()
    j = r.json()
    return j["access_token"], j.get("refresh_token")


def rotate(new_rt):
    if not new_rt or new_rt == RTOKEN:
        return
    try:
        from nacl import public, encoding
        pk = requests.get(f"https://api.github.com/repos/{SELF}/actions/secrets/public-key", headers=GH).json()
        sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder)).encrypt(new_rt.encode())
        requests.put(f"https://api.github.com/repos/{SELF}/actions/secrets/GRAPH_REFRESH_TOKEN", headers=GH,
                     json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": pk["key_id"]}).raise_for_status()
        print("Rotated GRAPH_REFRESH_TOKEN.")
    except Exception as e:
        print("WARN: could not rotate refresh token:", e)


def _norm(s):
    return re.sub(r"[\s_]+", " ", s.lower())


def _start(name):
    """Range start (yy,mm,dd) from 'Dated MM_DD_YY - MM_DD_YY'; huge if absent so
    today-only exports never beat the full-year (YTD) file."""
    m = re.search(r"(\d{2})_(\d{2})_(\d{2})\s*-\s*\d{2}_\d{2}_\d{2}", name)
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (99, 99, 99)


def children(tok):
    url = (f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(FOLDER)}:/children"
           "?$top=400&$select=id,name,lastModifiedDateTime")
    out = []
    while url:
        j = requests.get(url, headers={"Authorization": "Bearer " + tok}).json()
        out += j.get("value", [])
        url = j.get("@odata.nextLink")
    return out


def main():
    tok, new_rt = graph_token()
    rotate(new_rt)
    items = children(tok)
    os.makedirs("SILO_Reports", exist_ok=True)
    missing = []
    for canon, phrase in PHRASES.items():
        cands = [it for it in items
                 if it["name"].lower().endswith(".xlsx") and phrase in _norm(it["name"])]
        if not cands:
            missing.append(phrase)
            print("MISSING:", phrase)
            continue
        earliest = min(_start(it["name"]) for it in cands)              # widest YTD range
        best = max((it for it in cands if _start(it["name"]) == earliest),
                   key=lambda it: it["lastModifiedDateTime"])            # newest of those
        data = requests.get(
            f"https://graph.microsoft.com/v1.0/me/drive/items/{best['id']}/content",
            headers={"Authorization": "Bearer " + tok}).content
        with open(os.path.join("SILO_Reports", canon), "wb") as f:
            f.write(data)
        print(f"{canon} <- {best['name']} ({len(data)} bytes)")
    if missing:
        raise SystemExit("Missing reports in OneDrive: " + ", ".join(missing))


if __name__ == "__main__":
    main()
