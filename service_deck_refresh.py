#!/usr/bin/env python3
"""Cloud rebuild of the "classic deck" embedded SERVICE_DATA blob (service.html +
test_service.html in the sierra-ropp-dashboard repo), mirroring how daily.yml
rebuilds the ROPP deck's index.html in GitHub Actions — no machine needed.

Pipeline:
  1. Seed ~/.servicetitan/sierra.json from ST_CREDS_JSON env if not already present
     (same pattern as cloud_hourly.py / service_data.py's cloud_bootstrap_creds()).
  2. Refresh the Service report caches (cache/service_fieldconv.json,
     service_v1.json, service_memberships.json, service_iaq.json,
     service_monthly.json) live from the ServiceTitan Reporting API — these are a
     hard requirement for service_live.build(), not an optimization (it reads them
     unconditionally via _load()). Reuses staging_fetch.py's fetch_service() /
     fetch_service_monthly() so the fetch logic isn't duplicated.
  3. Import service_live and call its build() function (refactor-import, not a
     copy) to get the SERVICE_DATA dict.
  4. Fetch the CURRENT service.html + test_service.html from the
     sierra-ropp-dashboard repo via the GitHub contents API, replace ONLY the
     single-line `const SERVICE_DATA = {...};` statement via regex, verifying
     exactly one match per file before touching anything else in either file.
  5. PUT both files back via the contents API (DASHBOARD_TOKEN), commit message
     "Cloud service deck refresh <date>".

Fails loudly (nonzero exit) on any mismatch (regex count != 1, missing creds,
API error) rather than publishing a broken page.

Usage:
  python service_deck_refresh.py            # full run: fetch, build, publish
  python service_deck_refresh.py --dry       # build blob + verify regex match
                                              # against the live public HTML,
                                              # print diagnostics, publish nothing
"""
import base64, json, os, re, sys, urllib.request, urllib.error, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TZ = ZoneInfo("America/Los_Angeles")
PUB_REPO = "johnschwinghamer94-lab/sierra-ropp-dashboard"
API = "https://api.github.com/repos/" + PUB_REPO + "/contents/"
RAW = "https://raw.githubusercontent.com/" + PUB_REPO + "/main/"
TARGETS = ["service.html", "test_service.html"]


def cloud_bootstrap_creds():
    """Seed ~/.servicetitan/sierra.json from ST_CREDS_JSON env if it doesn't
    already exist (same pattern as service_data.py / cloud_hourly.py)."""
    fp = Path.home() / ".servicetitan" / "sierra.json"
    if not fp.exists() and os.environ.get("ST_CREDS_JSON", "").strip():
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(os.environ["ST_CREDS_JSON"])
        print("seeded ~/.servicetitan/sierra.json from ST_CREDS_JSON")


def refresh_caches():
    """Refresh only the Service report caches (skips staging_fetch's CA reports —
    not needed for the classic SERVICE_DATA blob)."""
    import staging_fetch as sf
    today, ytd_from, _mtd_from = sf._date_range()
    print(f"Refreshing Service caches — YTD {ytd_from} -> {today}")
    sf.fetch_service(today, ytd_from)
    sf.fetch_service_monthly(today)


def build_blob():
    import service_live
    D = service_live.build()
    blob = json.dumps(D, separators=(",", ":"))
    assert "</script" not in blob.lower(), "unsafe </script sequence in SERVICE_DATA JSON"
    return D, blob


def _gh_headers(extra=None):
    h = {"Authorization": "token " + os.environ["DASHBOARD_TOKEN"],
         "Accept": "application/vnd.github+json",
         "User-Agent": "service-deck-refresh"}
    if extra:
        h.update(extra)
    return h


def gh_fetch(path):
    """Full contents-API fetch (returns text + sha) — used for the real publish
    path since it requires the sha for the PUT anyway."""
    req = urllib.request.Request(API + path, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    return base64.b64decode(j["content"]).decode("utf-8"), j["sha"]


def raw_fetch(path):
    """No-auth fetch of the current PUBLIC file contents (used for --dry so it
    works without DASHBOARD_TOKEN)."""
    req = urllib.request.Request(RAW + path, headers={"User-Agent": "service-deck-refresh"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


PAT = re.compile(r"^const SERVICE_DATA = .*;$", re.MULTILINE)


def check_single_match(html, label):
    n = len(PAT.findall(html))
    if n != 1:
        raise RuntimeError(f"{label}: expected exactly 1 occurrence of 'const SERVICE_DATA = ...;', found {n}")
    return n


def splice(html, blob):
    return PAT.sub(lambda m: f"const SERVICE_DATA = {blob};", html, count=1)


def gh_put(path, text, sha, msg):
    body = {"message": msg, "branch": "main",
            "content": base64.b64encode(text.encode("utf-8")).decode()}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="PUT",
                                  headers=_gh_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read())


def main():
    dry = "--dry" in sys.argv

    cloud_bootstrap_creds()
    refresh_caches()
    D, blob = build_blob()

    print(f"blob size: {len(blob):,} bytes")
    print(f"dateRange: {D['dateRange']}")
    print(f"mtdRange:  {D['mtdRange']}")

    if dry:
        # verify regex match against the current live public HTML — no auth
        # needed, no writes anywhere.
        for fname in TARGETS:
            html = raw_fetch(fname)
            check_single_match(html, fname)
            print(f"  {fname}: regex OK (1 match), current length {len(html):,} bytes")
        print("DRY RUN — nothing published.")
        return

    if "DASHBOARD_TOKEN" not in os.environ:
        print("FATAL: DASHBOARD_TOKEN not set — cannot publish.", file=sys.stderr)
        sys.exit(1)

    today = dt.datetime.now(TZ).strftime("%Y-%m-%d")
    msg = f"Cloud service deck refresh {today}"

    # Fetch + verify BOTH files before writing anything (abort without
    # publishing if either fails the single-match check).
    fetched = {}
    for fname in TARGETS:
        html, sha = gh_fetch(fname)
        check_single_match(html, fname)
        fetched[fname] = (html, sha)
        print(f"  {fname}: fetched ({len(html):,} bytes), regex OK (1 match)")

    for fname in TARGETS:
        html, sha = fetched[fname]
        new_html = splice(html, blob)
        gh_put(fname, new_html, sha, msg)
        print(f"  {fname}: published ({len(new_html):,} bytes)")

    print("DONE.")


if __name__ == "__main__":
    main()
