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
import re
import sys
import time
from datetime import datetime, timedelta, timezone

BANDS = {"Strong", "Strong on wins", "Solid", "Moderate", "Weak"}
OUTCOMES = {"closed", "flipped", "no-close", "unknown"}


def norm_tech(name):
    """Canonical tech name: underscores and runs of whitespace collapse to one space.

    The live-coach writer emits the same rep under several spellings -- 'Joe Mendoza'
    and 'Joe_Mendoza', 'Nathan Colquitt' / 'Nathan_Colquitt' / 'Nathan  Colquitt'
    (double space). The scorecard view groups by this field, so each variant renders
    as a separate person: on 2026-08-03 the feed carried 17 spellings for 12 techs and
    Joe Mendoza's 10 cards showed up as two reps with 4 and 6.

    Normalizing here rather than in the writer heals the whole feed on the next relay
    -- including cards already published -- and keeps working if the writer starts
    emitting a new variant. Only whitespace and underscores are touched, so
    'AJ-Alejandro Ruiz Padilla', 'Alex - Oleksiy Yakovchuk' and 'Mike (Jiangtao) Li'
    survive intact.
    """
    return re.sub(r"[\s_]+", " ", name).strip() if isinstance(name, str) else name


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

    if c.get("tech"):
        c["tech"] = norm_tech(c["tech"])

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

    # carried-forward cards predate normalization, so heal them too -- otherwise a
    # card that stays inside the 48h window but gets no fresh livecard keeps its old
    # spelling and goes on splitting that rep in the view
    healed = 0
    for c in cur:
        if c.get("tech"):
            fixed = norm_tech(c["tech"])
            if fixed != c["tech"]:
                c["tech"] = fixed
                healed += 1

    # Dedupe case-insensitively: a case-mangled recId twin must replace, not
    # duplicate, its sibling card. Keys are case-folded to match the lookup below.
    byid = {}
    for c in cur:
        byid[str(c["recId"]).lower()] = c
    for c in new:
        k = str(c["recId"]).lower()
        prev = byid.get(k)
        if prev is None or c.get("dateCreated", "") >= prev.get("dateCreated", ""):
            byid[k] = c

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

    techs = {c["tech"] for c in cards if c.get("tech")}
    print(f"scorecards.json: {len(cards)} cards ({len(new)} new, {len(cur)} prior) "
          f"across {len(techs)} techs"
          + (f"; normalized {healed} carried-forward tech name(s)" if healed else ""))


if __name__ == "__main__":
    main()
