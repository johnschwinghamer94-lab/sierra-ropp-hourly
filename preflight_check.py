#!/usr/bin/env python3
"""Fast preflight guard for the Cloud rebuild.

Runs in seconds at the top of daily.yml so a stale push that reverts the cancellation
work fails the build IMMEDIATELY with a clear message — instead of a 13-minute rebuild
that either crashes on a missing function or silently drops the exclusion.

It protects two things that MUST travel together:
  1. The Cancellation-tab duplicate-exclusion logic in UPDATE_DASHBOARD.py.
  2. patch_cancel_markup defined in the SAME file that calls it (auto_update_dashboard.py),
     so a partial revert can never orphan the call again.

If you are intentionally removing this feature, delete this file and its daily.yml step.
"""
import sys, importlib
from pathlib import Path

HERE = Path(__file__).parent
FAIL = []

def src(name):
    return (HERE / name).read_text(encoding="utf-8")

# 1. patch_cancel_markup: call and definition must be in the SAME file (auto_update_dashboard.py)
aud = src("auto_update_dashboard.py")
calls_markup = "patch_cancel_markup(" in aud.replace("def patch_cancel_markup(", "")
defines_markup = "def patch_cancel_markup(" in aud
if calls_markup and not defines_markup:
    FAIL.append("auto_update_dashboard.py CALLS patch_cancel_markup but does not DEFINE it "
                "(the exact orphan that crashed every rebuild). A stale checkout likely "
                "reverted the definition — pull --rebase on the machine that pushed, then re-apply.")
if defines_markup and not calls_markup:
    FAIL.append("auto_update_dashboard.py defines patch_cancel_markup but never calls it in build_html.")
if "U.patch_cancel_markup" in aud:
    FAIL.append("auto_update_dashboard.py references U.patch_cancel_markup (cross-module) — it must "
                "call the LOCAL patch_cancel_markup so the call and definition can never drift apart.")

# 2. Cancellation exclusion logic must be present in UPDATE_DASHBOARD.py
ud = src("UPDATE_DASHBOARD.py")
for marker, what in [("_load_cancel_exclusions", "the exclusion-list loader"),
                     ("in CANCEL_EXCLUDE", "the cancel-loop skip"),
                     ("dupExcluded", "the build_cancel dup count/note")]:
    if marker not in ud:
        FAIL.append("UPDATE_DASHBOARD.py is missing %s (`%s`) — the duplicate-cancellation "
                    "exclusion was reverted. Likely a stale push." % (what, marker))

# 3. Exclusion data file must exist and parse
try:
    import json
    json.loads((HERE / "cancel_dup_exclusions.json").read_text())
except Exception as e:
    FAIL.append("cancel_dup_exclusions.json is missing or invalid (%s)." % e)

# 4. Both modules must import cleanly (catches syntax / load-time errors)
for mod in ("UPDATE_DASHBOARD", "auto_update_dashboard"):
    try:
        importlib.import_module(mod)
    except Exception as e:
        FAIL.append("import %s failed: %s: %s" % (mod, type(e).__name__, e))

if FAIL:
    print("PREFLIGHT FAILED — the cancellation exclusion / markup is inconsistent:\n", file=sys.stderr)
    for f in FAIL:
        print("  ✗ " + f, file=sys.stderr)
    print("\nFix: `git pull --rebase` on every machine before editing this repo, then re-apply. "
          "See the commit 'Fix rebuild crash: restore cancel-exclusion logic'.", file=sys.stderr)
    sys.exit(1)

print("preflight OK — cancellation exclusion + self-contained markup present and consistent.")
