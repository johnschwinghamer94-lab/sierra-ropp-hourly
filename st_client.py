#!/usr/bin/env python3
"""Minimal ServiceTitan API client (stdlib only). Reads creds from the git-ignored
~/.servicetitan/sierra.json; never prints the secret. OAuth2 client_credentials."""
import json, os, time, urllib.request, urllib.parse, urllib.error

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CREDS_PATH = os.path.expanduser("~/.servicetitan/sierra.json")
CACHE_PATH = os.path.expanduser("~/.servicetitan/sierra_token.json")

def _creds():
    with open(CREDS_PATH) as f:
        return json.load(f)

def get_token(force=False):
    c = _creds()
    if not force and os.path.exists(CACHE_PATH):
        try:
            t = json.load(open(CACHE_PATH))
            if t.get("expires_at", 0) - 60 > time.time():
                return t["access_token"]
        except Exception:
            pass
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
    }).encode()
    req = urllib.request.Request(c["auth_base"].rstrip("/") + "/connect/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.load(r)
    tok["expires_at"] = time.time() + tok.get("expires_in", 900)
    with open(os.open(CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(tok, f)
    return tok["access_token"]

def _headers(c, extra=None):
    h = {"Authorization": "Bearer " + get_token(), "ST-App-Key": c["app_key"], "User-Agent": UA}
    if extra:
        h.update(extra)
    return h

def api_get(path, params=None, tenant="SIE"):
    c = _creds()
    url = c["api_base"].rstrip("/") + path.replace("{tenant}", c["tenants"][tenant])
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(c))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def api_post(path, body, params=None, tenant="SIE"):
    c = _creds()
    url = c["api_base"].rstrip("/") + path.replace("{tenant}", c["tenants"][tenant])
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
        headers=_headers(c, {"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

def run_report(category, report_id, parameters, page=1, page_size=5000, tenant="SIE"):
    """Run a saved report; returns the raw {fields, data, hasMore, ...} payload."""
    path = f"/reporting/v2/tenant/{{tenant}}/report-category/{category}/reports/{report_id}/data"
    return api_post(path, {"parameters": parameters}, {"page": page, "pageSize": page_size}, tenant)

if __name__ == "__main__":
    import sys
    try:
        print("AUTH OK — token length", len(get_token()))
        bu = api_get("/settings/v2/tenant/{tenant}/business-units", {"page": 1, "pageSize": 5})
        print("sample business units:", [b.get("name") for b in bu.get("data", [])[:5]])
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.reason, "->", e.read().decode()[:300]); sys.exit(1)
