#!/usr/bin/env python3
"""Merge live-coach card JSONs into scorecards.json for the cloud relay.

Reads every *.card.json file in <livecards_dir>, validates it, and merges it
into the scorecards file at <scorecards_json_path>: dedupe by recId, keep the
last 48h, sort newest first, write atomically.

Usage:
    python tools/merge_livecards.py <livecards_dir> <scorecards_json_path>

Invalid cards are skipped with a printed warning; the merge continues.
Port of CLAUDE STUFF/LIVE COACH/merge_cards.py + extract_card.py validation.
"""
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BANDS = {"Strong", "Strong on wins", "Solid", "Moderate", "Weak"}
OUTCOMES = {"closed", "flipped", "no-close", "unknown"}


def load_card(path):
    try:
        c = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"warning: {path}: could not parse JSON ({e}) — skipping")
        return None

    if not isinstance(c, dict):
        print(f"warning: {path}: not a JSON object — skipping")
        return None

    if c.get("skip"):
        return None

    if "recId" not in c:
        print(f"warning: {path}: missing recId — skipping")
        return None

    bands = c.get("bands", {})
    if not all(bands.get(k) in BANDS for k in ("welcome", "assessment", "decision", "deliver")):
        print(f"warning: {path}: invalid or missing band value — skipping")
        return None

    if c.get("outcome") not in OUTCOMES:
        c["outcome"] = "unknown"

    return c


def main():
    if len(sys.argv) != 3:
        print("usage: python tools/merge_livecards.py <livecards_dir> <scorecards_json_path>")
        sys.exit(1)

    livecards_dir = sys.argv[1]
    dst = sys.argv[2]

    new = []
    for p in sorted(glob.glob(os.path.join(livecards_dir, "*.card.json"))):
        c = load_card(p)
        if c is not None:
            new.append(c)

    try:
        cur = json.load(open(dst, encoding="utf-8")).get("cards", [])
    except Exception:
        cur = []

    byid = {c["recId"]: c for c in cur}
    for c in new:
        byid[c["recId"]] = c

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    cards = [c for c in byid.values() if c.get("dateCreated", "") >= cutoff]
    cards.sort(key=lambda c: c.get("dateCreated", ""), reverse=True)

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generatedMs": int(time.time() * 1000),
        "cards": cards,
    }

    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, separators=(",", ":")))
    os.replace(tmp, dst)

    print(f"scorecards.json: {len(cards)} cards ({len(new)} new, {len(cur)} prior)")


if __name__ == "__main__":
    main()
