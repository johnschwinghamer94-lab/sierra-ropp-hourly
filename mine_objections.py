#!/usr/bin/env python3
"""Objection mining per coaching/objection_mining_prompt.md"""
import os
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import sys

REPO_ROOT = Path(__file__).parent
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"
OBJECTIONS_DIR = REPO_ROOT / "objections"
TGL_TRUTH_DIR = REPO_ROOT / "tgl_truth"
STATE_FILE = OBJECTIONS_DIR / "mined_state.json"
SUMMARY_FILE = OBJECTIONS_DIR / "summary.json"

OBJECTION_TYPES = [
    "Price",
    "Stall / Think About It",
    "Doubt / Distrust",
    "System Too Old",
    "Competitor",
    "Spouse / Decision-Maker",
    "Other",
]

def reflow_transcript(lines):
    """Reflow word-per-line transcript into sentences by speaker turn."""
    result = []
    current_speaker = None
    current_words = []

    for line in lines:
        line = line.rstrip()
        if not line:
            continue

        # Check if line is a speaker label (ends with colon, possibly indented)
        stripped = line.lstrip()
        if stripped.endswith(":") and not stripped.startswith(" "):
            # This is a speaker label
            if current_speaker and current_words:
                # Flush previous speaker
                result.append((current_speaker, " ".join(current_words)))
            current_speaker = stripped[:-1]  # Remove trailing colon
            current_words = []
        else:
            # This is a word/continuation
            if current_speaker and stripped:
                current_words.append(stripped)

    # Flush final speaker
    if current_speaker and current_words:
        result.append((current_speaker, " ".join(current_words)))

    return result

def parse_transcript_header(path):
    """Extract Rep, Date, Duration, RecId, Job from transcript header."""
    header = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip()
            if not line or line.startswith("="):
                break
            if ":" in line:
                parts = line.split(":", 1)
                key = parts[0].strip()
                val = parts[1].strip() if len(parts) > 1 else ""
                header[key] = val
    return header

def load_tgl_truth(date_str):
    """Load TGL flip truth for a date."""
    path = TGL_TRUTH_DIR / f"{date_str}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def load_ytd_sources():
    """Load YTD sources as fallback."""
    path = TGL_TRUTH_DIR / "ytd_sources.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def is_objection(text):
    """Check if text contains an objection."""
    text_lower = text.lower()

    objection_patterns = [
        r"\bprice\b|\bcost\b|\bexpensive\b|\btoo much\b|\bcan't afford\b|\bbudget\b",
        r"\bstall\b|\bthink about\b|\bthink it over\b|\bcall you back\b|\blet me think\b|\bneed time\b",
        r"\bdoubt\b|\bdistrust\b|\bnot sure\b|\bnot confident\b|\nskeptical\b|\bworry\b|\bconcern\b",
        r"\bold\b|\bsystem too old\b|\bneeds replacing\b|\bdated\b",
        r"\bcompetitor\b|\bother company\b|\belse\b|\bquote\b",
        r"\bspouse\b|\bwife\b|\bhusband\b|\bpartner\b|\bsignificant other\b|\bdecision maker\b",
    ]

    for pattern in objection_patterns:
        if re.search(pattern, text_lower):
            return True
    return False

def classify_objection(cust_text, answer_text):
    """Classify objection into one of 7 types."""
    combined = (cust_text + " " + (answer_text or "")).lower()

    if re.search(r"\bprice\b|\bcost\b|\bexpensive\b|\btoo much\b|\bbudget\b", combined):
        return "Price"
    if re.search(r"\bthink.*about\b|\bstall\b|\bcall.*back\b|\blet me think\b", combined):
        return "Stall / Think About It"
    if re.search(r"\bdoubt\b|\bdistrust\b|\bskeptic\b|\bnot sure\b|\bworry\b", combined):
        return "Doubt / Distrust"
    if re.search(r"\bold\b|\bsystem.*old\b|\bneeds.*replac\b", combined):
        return "System Too Old"
    if re.search(r"\bcompetitor\b|\bother.*company\b|\belse\b|\bquote\b", combined):
        return "Competitor"
    if re.search(r"\bspouse\b|\bwife\b|\bhusband\b|\bpartner\b|\bdecision.*maker\b", combined):
        return "Spouse / Decision-Maker"
    return "Other"

def grade_response(cust_text, answer_text):
    """Grade the tech's response: great/ok/poor"""
    if not answer_text or not answer_text.strip():
        return "poor"

    answer_lower = answer_text.lower()

    # Check for markers of good responses
    has_value_tie = re.search(r"\bvalue\b|\breturn\b|\binvestment\b|\bsave\b|\blong[- ]term\b", answer_lower)
    has_isolation = re.search(r"\b(so|let me)\b.*\b(understand|clarify|confirm)\b", answer_lower)
    has_advance = re.search(r"\bspecialist\b|\bvisit\b|\bbook\b|\bschedule\b|\bnext\b", answer_lower)

    if has_isolation and (has_value_tie or has_advance):
        return "great"

    # Check for capitulation or argument
    if re.search(r"(okay|you.?re right|i understand).*\b(won't|can't)\b", answer_lower):
        return "poor"
    if re.search(r"\bbut\b.*\byou\b.*\bwrong\b", answer_lower):
        return "poor"

    # Generic but present answer
    if len(answer_text) > 20:
        return "ok"

    return "poor"

def check_closed(job_num, call_date):
    """Check if job closed in TGL truth."""
    if not job_num:
        return False

    # Extract numeric job ID from job_num (may contain text)
    numeric_id = None
    for part in job_num.split():
        if part.isdigit() and len(part) >= 9:
            numeric_id = part
            break
    if not numeric_id:
        return False

    # Check same-day truth
    truth = load_tgl_truth(call_date)
    tgls = truth.get("tgls", {})
    if numeric_id in tgls:
        return True

    # Check next-day truth
    try:
        next_date = (datetime.fromisoformat(call_date) + timedelta(days=1)).isoformat()[:10]
        truth = load_tgl_truth(next_date)
        tgls = truth.get("tgls", {})
        if numeric_id in tgls:
            return True
    except:
        pass

    # Check YTD sources
    ytd = load_ytd_sources()
    sources = ytd.get("sources", {})
    if numeric_id in sources:
        return True

    return False

def extract_objections_from_folder(folder_path):
    """Extract all objections from a transcript folder."""
    objections = []

    for txt_file in sorted(folder_path.glob("*.txt")):
        header = parse_transcript_header(txt_file)
        rep_name = header.get("Rep", "Unknown")
        call_date = header.get("Date", "")
        rec_id = header.get("RecId", txt_file.stem)[:16] if "RecId" in header else txt_file.stem
        job_num = header.get("Job", "")

        # Parse and reflow transcript
        with open(txt_file) as f:
            all_lines = f.read().split("\n")

        # Skip header lines (until we see empty line)
        header_end = 0
        for i, line in enumerate(all_lines):
            if not line.strip() or line.startswith("="):
                header_end = i + 1
                break

        body_lines = all_lines[header_end:]
        turns = reflow_transcript(body_lines)

        if not turns:
            continue

        # Find customer turns (look for "Customer" speaker)
        customer_turns_idx = []
        for i, (speaker, text) in enumerate(turns):
            if "customer" in speaker.lower():
                customer_turns_idx.append(i)

        objection_count = 0

        for cust_idx in customer_turns_idx:
            speaker, cust_turn = turns[cust_idx]
            if is_objection(cust_turn):
                objection_count += 1

                # Find next turn (tech response)
                tech_response = ""
                if cust_idx + 1 < len(turns):
                    _, tech_response = turns[cust_idx + 1]

                # Trim quotes to ~50 and ~60 words
                cust_quote = cust_turn[:200]  # Rough character limit
                answer_quote = tech_response[:250] if tech_response else ""

                objection_type = classify_objection(cust_quote, answer_quote)
                grade = grade_response(cust_quote, answer_quote)
                closed = check_closed(job_num, call_date)

                obj_id = f"{rec_id}-{objection_count}"
                objections.append({
                    "id": obj_id,
                    "type": objection_type,
                    "cust": cust_quote,
                    "answer": answer_quote,
                    "tech": rep_name,
                    "date": call_date,
                    "grade": grade,
                    "closed": closed,
                    "job": job_num,
                })

    return objections

def merge_into_month_file(objections):
    """Merge objections into appropriate month files."""
    by_month = {}

    for obj in objections:
        date_str = obj["date"]
        try:
            month = date_str[:7]  # YYYY-MM
        except:
            continue

        if month not in by_month:
            by_month[month] = []
        by_month[month].append(obj)

    for month, objs in by_month.items():
        month_file = OBJECTIONS_DIR / f"{month}.json"

        # Load existing
        existing = {"month": month, "items": []}
        if month_file.exists():
            with open(month_file) as f:
                existing = json.load(f)

        # Dedupe by id
        id_map = {item["id"]: item for item in existing["items"]}
        for obj in objs:
            id_map[obj["id"]] = obj

        existing["items"] = list(id_map.values())

        with open(month_file, "w") as f:
            json.dump(existing, f, indent=2)

def regenerate_summary():
    """Regenerate summary.json from all month files."""
    all_items = []
    months = []

    for month_file in sorted(OBJECTIONS_DIR.glob("*.json"), reverse=True):
        if month_file.name in ["mined_state.json", "summary.json"]:
            continue

        with open(month_file) as f:
            data = json.load(f)
            months.append(data["month"])
            all_items.extend(data.get("items", []))

    # Compute stats
    totals = {"total": len(all_items), "great": 0, "ok": 0, "poor": 0}
    types_stats = {t: {"total": 0, "great": 0, "ok": 0, "poor": 0} for t in OBJECTION_TYPES}

    for item in all_items:
        grade = item["grade"]
        totals[grade] += 1

        t = item["type"]
        if t in types_stats:
            types_stats[t]["total"] += 1
            types_stats[t][grade] += 1

    # Find date window
    dates = [item["date"] for item in all_items if "date" in item]
    window = {"start": min(dates), "end": max(dates)} if dates else {"start": "", "end": ""}

    summary = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "window": window,
        "totals": totals,
        "types": types_stats,
        "months": months,
    }

    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)

def main():
    # Get folders to mine
    mined_state = {}
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            mined_state = json.load(f)

    all_folders = sorted(TRANSCRIPTS_DIR.glob("*/"))
    to_mine = [f for f in all_folders if f.name not in mined_state][:20]

    if not to_mine:
        print("No folders to mine")
        return

    all_objections = []

    for folder in to_mine:
        try:
            print(f"Mining {folder.name}...", file=sys.stderr)
            objs = extract_objections_from_folder(folder)
            all_objections.extend(objs)
            mined_state[folder.name] = datetime.utcnow().isoformat() + "Z"
        except Exception as e:
            print(f"Error mining {folder.name}: {e}", file=sys.stderr)

    # Merge into month files
    merge_into_month_file(all_objections)

    # Regenerate summary
    regenerate_summary()

    # Update state
    with open(STATE_FILE, "w") as f:
        json.dump(mined_state, f, indent=2)

    # Report
    obj_types = {}
    grades = {"great": 0, "ok": 0, "poor": 0}
    for obj in all_objections:
        t = obj["type"]
        obj_types[t] = obj_types.get(t, 0) + 1
        grades[obj["grade"]] += 1

    remaining = len(all_folders) - len(mined_state)

    print(f"\n=== OBJECTION MINING REPORT ===")
    print(f"Folders mined: {len(to_mine)}")
    print(f"Date range: {to_mine[0].name} to {to_mine[-1].name}")
    print(f"Total objections found: {len(all_objections)}")
    print(f"\nBy type:")
    for t in OBJECTION_TYPES:
        count = obj_types.get(t, 0)
        print(f"  {t}: {count}")
    print(f"\nGrades distribution:")
    print(f"  great: {grades['great']}")
    print(f"  ok: {grades['ok']}")
    print(f"  poor: {grades['poor']}")
    print(f"\nFolders remaining unmined: {remaining}")

if __name__ == "__main__":
    main()
