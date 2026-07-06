#!/usr/bin/env python3
"""
graph_setup.py — RUN ONCE on any machine (your PC is fine) to authorize the cloud
capture to read your OneDrive. Uses the device-code flow, so you just paste a code
into a browser and sign in with your work account. Prints the GRAPH_REFRESH_TOKEN to
store as a GitHub secret. After this you never need a machine again.

Usage:
    py graph_setup.py <CLIENT_ID> <TENANT_ID>
(CLIENT_ID + TENANT_ID come from your Azure app registration.)
"""
import sys, time, requests

if len(sys.argv) < 3:
    print("Usage: py graph_setup.py <CLIENT_ID> <TENANT_ID>")
    sys.exit(1)
CLIENT, TENANT = sys.argv[1], sys.argv[2]
SCOPE = "Files.Read offline_access"
BASE = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"

dc = requests.post(f"{BASE}/devicecode", data={"client_id": CLIENT, "scope": SCOPE}).json()
if "user_code" not in dc:
    print("Error starting device flow:", dc); sys.exit(1)

print("\n" + "=" * 60)
print("  1. Open:  " + dc["verification_uri"])
print("  2. Enter code:  " + dc["user_code"])
print("  3. Sign in with your Sierra work account and approve.")
print("=" * 60 + "\n")
print("Waiting for you to finish in the browser...")

interval = int(dc.get("interval", 5))
deadline = time.time() + int(dc.get("expires_in", 900))
while time.time() < deadline:
    time.sleep(interval)
    tok = requests.post(f"{BASE}/token", data={
        "client_id": CLIENT, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": dc["device_code"]}).json()
    err = tok.get("error")
    if err == "authorization_pending":
        continue
    if err == "slow_down":
        interval += 5; continue
    if err:
        print("Error:", tok.get("error_description", err)); sys.exit(1)
    rt = tok.get("refresh_token")
    if rt:
        print("\nSUCCESS. Add this GitHub secret to the sierra-ropp-hourly repo:\n")
        print("  Name:  GRAPH_REFRESH_TOKEN")
        print("  Value:\n")
        print(rt + "\n")
        sys.exit(0)

print("Timed out. Re-run and finish the browser step faster.")
sys.exit(1)
