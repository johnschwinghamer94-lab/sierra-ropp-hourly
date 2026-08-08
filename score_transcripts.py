#!/usr/bin/env python3
"""Extract metadata and reflow SERVICE call transcripts for scoring."""

import os
import re
import json
from pathlib import Path
from datetime import datetime, timedelta

def extract_header_info(lines):
    """Extract RecId, Job, Date, Duration from transcript header."""
    info = {
        'recId': None,
        'job': None,
        'date': None,
        'duration': None,
        'rep': None,
        'body_start': 0
    }

    for i, line in enumerate(lines[:20]):  # Check first 20 lines
        line = line.strip()
        if line.startswith('RecId:'):
            info['recId'] = line.replace('RecId:', '').strip()
        elif line.startswith('Job:'):
            job_text = line.replace('Job:', '').strip()
            match = re.search(r'Job\s*#\s*(\d+)', job_text)
            if match:
                info['job'] = match.group(1)
        elif line.startswith('Date:'):
            info['date'] = line.replace('Date:', '').strip()
        elif line.startswith('Duration:'):
            info['duration'] = line.replace('Duration:', '').strip()
        elif line.startswith('Rep:'):
            info['rep'] = line.replace('Rep:', '').strip()
        elif line == '=' * 10:  # separator line
            info['body_start'] = i + 1
            break

    return info

def reflow_transcript(lines):
    """Reflow transcript from one-word-per-line to readable speaker turns."""
    result = []
    current_speaker = None
    current_text = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith('Speaker '):
            # Save previous speaker block
            if current_speaker is not None and current_text:
                result.append(f"{current_speaker}\n  {' '.join(current_text)}\n")

            current_speaker = line + ':'
            current_text = []
        else:
            current_text.append(line)

    # Save last speaker block
    if current_speaker is not None and current_text:
        result.append(f"{current_speaker}\n  {' '.join(current_text)}\n")

    return ''.join(result)

def build_pseudo_id(filename):
    """Build noid_<filename> pseudo-id for transcripts without RecId."""
    base = filename.replace('.txt', '')
    # Replace non-alphanumeric (except . _ -) with underscore, collapse runs
    normalized = re.sub(r'[^a-zA-Z0-9._-]+', '_', base)
    normalized = re.sub(r'_+', '_', normalized)
    return f"noid_{normalized}"

def process_transcript(filepath):
    """Process a single transcript file."""
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    info = extract_header_info(lines)
    body_lines = lines[info['body_start']:]
    body_text = reflow_transcript(body_lines)

    # Build recId if missing
    if not info['recId']:
        info['recId'] = build_pseudo_id(os.path.basename(filepath))

    # Extract date from filename if header date is missing
    if not info['date']:
        match = re.search(r'transcripts/(\d{4}-\d{2}-\d{2})/', filepath)
        if match:
            info['date'] = match.group(1)

    # Parse duration
    if info['duration']:
        match = re.search(r'(\d+)', info['duration'])
        if match:
            info['duration'] = int(match.group(1))

    return {
        'filepath': filepath,
        'metadata': info,
        'body': body_text
    }

def list_unscored(transcript_dir='transcripts', scored_dir='livecards_service'):
    """List all unscored SERVICE transcripts, oldest first."""
    transcripts = []

    # Find all svc__ transcripts
    for date_dir in sorted(Path(transcript_dir).iterdir()):
        if not date_dir.is_dir():
            continue
        for transcript in sorted(date_dir.glob('svc__*.txt')):
            transcripts.append(transcript)

    # Filter out already-scored ones (check case-insensitive)
    scored_recids = set()
    if Path(scored_dir).exists():
        for card in Path(scored_dir).glob('*.card.json'):
            try:
                with open(card) as f:
                    data = json.load(f)
                    scored_recids.add(data['recId'].lower())
            except:
                pass

    unscored = []
    for transcript in transcripts:
        info = extract_header_info(open(transcript).readlines()[:20])
        recid = info['recId'] or build_pseudo_id(transcript.name)
        if recid.lower() not in scored_recids:
            unscored.append(transcript)

    return unscored

if __name__ == '__main__':
    unscored = list_unscored()
    print(f"Found {len(unscored)} unscored transcripts (oldest first):")
    for t in unscored[:8]:  # Show first 8
        print(f"  {t}")
