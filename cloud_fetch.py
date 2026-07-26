#!/usr/bin/env python3
"""Cloud step: materialize ServiceTitan creds from the ST_CREDS_JSON secret and fetch
the ROPP reports into cache/. Exits non-zero on any problem so the workflow falls back
to the OneDrive Excel path. Prints diagnostics (never the secret itself)."""
import os, json, pathlib, sys

raw = os.environ.get("ST_CREDS_JSON") or ""
print(f"ST_CREDS_JSON length = {len(raw)}", flush=True)
if not raw.strip():
    print("ERROR: ST_CREDS_JSON secret is empty/whitespace"); sys.exit(3)
try:
    creds = json.loads(raw)
except Exception as e:
    print(f"ERROR: ST_CREDS_JSON is not valid JSON: {e}"); sys.exit(3)

d = pathlib.Path.home() / ".servicetitan"
d.mkdir(parents=True, exist_ok=True)
fp = d / "sierra.json"
fp.write_text(raw)
os.chmod(fp, 0o600)
print(f"creds written to {fp} ({len(creds)} keys: {', '.join(sorted(creds)[:6])})", flush=True)

import ropp_live
ropp_live.cache_reports()
n = len(list((pathlib.Path(ropp_live.HERE) / "cache").glob("*.json")))
print(f"cache_reports OK — {n} report caches written", flush=True)
