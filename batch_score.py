#!/usr/bin/env python3
"""Batch score SERVICE transcripts and create output files."""

import json
import re
from pathlib import Path
import sys
from auto_score import score_transcript, extract_header_and_body, format_title_case

def normalize_filename(text):
    """Normalize text for use in filenames."""
    # Replace runs of non-alphanumeric (except . _ -) with single underscore
    normalized = re.sub(r'[^a-zA-Z0-9._-]+', '_', text)
    # Collapse runs of underscores
    normalized = re.sub(r'_+', '_', normalized)
    # Remove trailing underscores
    normalized = normalized.rstrip('_')
    return normalized

def create_private_card(card, info, rep_display, transcript_path):
    """Create full private scorecard markdown."""
    date_folder = info['date']
    filename = f"scorecards_full/{date_folder}/svc__{card['recId']}__{normalize_filename(rep_display)}.md"

    # Read transcript for quotes
    with open(transcript_path, 'r', encoding='utf-8', errors='ignore') as f:
        transcript_text = f.read()

    content = f"""# Service Scorecard

**Tech:** {card['tech']}
**Date:** {card['dateCreated']}
**Duration:** {card['durationMin']} min
**Job:** {card['jobNumber'] or 'No job number'}
**RecId:** {card['recId']}

## Scoring

### Welcome
**Band:** {card['bands']['welcome']}

### Diagnosis
**Band:** {card['bands']['diagnosis']}

### Options
**Band:** {card['bands']['options']}

### Close
**Band:** {card['bands']['close']}

## Critical Actions

- Expectations Set: {card['critical']['expectations']}
- Options Presented (4+): {card['critical']['options']}
- Membership Offered: {card['critical']['membership']}
- Direct Close Asked: {card['critical']['close']}

## Outcome

{card['outcome'].upper()}

## Coaching Tip

{card['tip']}
"""
    return filename, content

def main():
    today = '2026-08-08'
    yesterday = '2026-08-07'

    # Get list of transcripts from both dates
    transcripts = []
    for date_dir in [f'transcripts/{yesterday}', f'transcripts/{today}']:
        if Path(date_dir).exists():
            for t in sorted(Path(date_dir).glob('svc__*.txt')):
                transcripts.append(t)

    # Filter to only unscored
    unscored = []
    scored_ids = set()

    if Path('livecards_service').exists():
        for card_file in Path('livecards_service').glob('*.card.json'):
            try:
                with open(card_file) as f:
                    data = json.load(f)
                    scored_ids.add(data['recId'].lower())
            except:
                pass

    for t in transcripts:
        info, _ = extract_header_and_body(t)
        if info['recId'].lower() not in scored_ids:
            unscored.append(t)

    # Score first 8
    print(f"Scoring {min(8, len(unscored))} of {len(unscored)} unscored transcripts...")

    scored_cards = []
    for i, transcript_path in enumerate(unscored[:8]):
        try:
            print(f"  {i+1}. {transcript_path.name[:50]}...", end=' ', flush=True)
            card, info, rep_display = score_transcript(str(transcript_path))
            scored_cards.append((card, info, rep_display, transcript_path))
            print("✓")
        except Exception as e:
            print(f"ERROR: {e}")

    # Create output directories
    Path('livecards_service').mkdir(exist_ok=True)
    for date in [yesterday, today]:
        Path(f'scorecards_full/{date}').mkdir(parents=True, exist_ok=True)

    # Write files
    print(f"\nWriting {len(scored_cards)} scorecard files...")
    for card, info, rep_display, transcript_path in scored_cards:
        # Write compact card
        compact_path = f"livecards_service/{card['recId']}.card.json"
        with open(compact_path, 'w') as f:
            json.dump(card, f)
        print(f"  {Path(compact_path).name}")

        # Write private card
        private_path, private_content = create_private_card(card, info, rep_display, transcript_path)
        Path(private_path).parent.mkdir(parents=True, exist_ok=True)
        with open(private_path, 'w') as f:
            f.write(private_content)
        print(f"  {Path(private_path).name}")

    print(f"\nScored: {len(scored_cards)}")
    print(f"Deferred: {max(0, len(unscored) - 8)} to next slot")

    return len(scored_cards), max(0, len(unscored) - 8)

if __name__ == '__main__':
    scored, deferred = main()
    sys.exit(0 if scored > 0 else 1)
