#!/usr/bin/env python3
"""Auto-score SERVICE call transcripts."""

import json
import re
from pathlib import Path
from datetime import datetime
import sys

def extract_header_and_body(filepath):
    """Extract metadata and body from transcript."""
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    info = {'recId': None, 'job': None, 'date': None, 'duration': None, 'rep': None}
    body_start = 0

    for i, line in enumerate(lines[:20]):
        line_strip = line.strip()
        if line_strip.startswith('RecId:'):
            info['recId'] = line_strip.replace('RecId:', '').strip()
        elif line_strip.startswith('Job:'):
            info['job'] = line_strip.replace('Job:', '').strip()
            # Extract job number
            match = re.search(r'Job\s*#\s*(\d+)', info['job'])
            info['job_number'] = match.group(1) if match else None
        elif line_strip.startswith('Date:'):
            info['date'] = line_strip.replace('Date:', '').strip()
        elif line_strip.startswith('Duration:'):
            duration_str = line_strip.replace('Duration:', '').strip()
            match = re.search(r'(\d+)', duration_str)
            info['duration'] = int(match.group(1)) if match else 0
        elif line_strip.startswith('Rep:'):
            info['rep'] = line_strip.replace('Rep:', '').strip()
        elif '=' * 5 in line_strip:
            body_start = i + 1
            break

    body_text = ''.join(lines[body_start:])

    # Build pseudo-id if needed
    if not info['recId']:
        filename = Path(filepath).name.replace('.txt', '')
        info['recId'] = f"noid_{filename}"

    return info, body_text

def reflow_body(body_text):
    """Convert one-word-per-line into readable paragraphs."""
    lines = [l.strip() for l in body_text.split('\n') if l.strip()]
    result = []
    current_speaker = None
    current_words = []

    for line in lines:
        if line.startswith('Speaker ') or line.endswith(':'):
            if current_speaker and current_words:
                result.append(f"{current_speaker}\n  {' '.join(current_words)}\n")
            current_speaker = line
            current_words = []
        else:
            current_words.append(line)

    if current_speaker and current_words:
        result.append(f"{current_speaker}\n  {' '.join(current_words)}\n")

    return ''.join(result)

def analyze_transcript(body_text, rep_name):
    """Analyze transcript to extract scores."""
    text_lower = body_text.lower()

    # Band assessment keywords
    welcome_keywords = ['hi', 'hello', 'good morning', 'how are', 'nice to meet']
    diagnosis_keywords = ['check', 'test', 'inspect', 'measure', 'reading', 'showing', 'found']
    options_keywords = ['option', 'better', 'upgrade', 'quote', 'price', 'estimate', 'install']
    close_keywords = ['ready to', 'go ahead', 'schedule', 'let\'s do it', 'today', 'approve']
    membership_keywords = ['membership', 'sam', 'maintenance', 'agreement', 'plan']

    # Count keywords
    welcome_count = sum(1 for kw in welcome_keywords if kw in text_lower)
    diagnosis_count = sum(1 for kw in diagnosis_keywords if kw in text_lower)
    options_count = sum(1 for kw in options_keywords if kw in text_lower)
    close_count = sum(1 for kw in close_keywords if kw in text_lower)
    membership_count = sum(1 for kw in membership_keywords if kw in text_lower)

    # Band scoring (based on keyword density and length)
    def score_band(count, min_for_strong=5, min_for_solid=3):
        if count >= min_for_strong:
            return "Strong"
        elif count >= min_for_solid:
            return "Solid"
        else:
            return "Weak"

    # Special handling for welcome (usually fewer keywords)
    if welcome_count >= 2:
        welcome_band = "Solid"
    elif welcome_count >= 1:
        welcome_band = "Solid"
    else:
        welcome_band = "Weak"

    diagnosis_band = score_band(diagnosis_count, 4, 2)
    options_band = score_band(options_count, 4, 2)
    close_band = score_band(close_count, 3, 1)

    # Critical actions
    expectations = 'cost' in text_lower or 'explain' in text_lower or 'proceed' in text_lower
    options = options_count >= 2 and 'four' in text_lower or 'multiple' in text_lower or 'better' in text_lower
    membership = membership_count > 0
    close = close_count >= 2 or ('approved' in text_lower or 'agree' in text_lower)

    # Outcome
    if 'close' in text_lower and close_count >= 2:
        outcome = "closed"
    elif 'decline' in text_lower or 'no thanks' in text_lower or 'later' in text_lower:
        outcome = "no-close"
    else:
        outcome = "unknown"

    return {
        'bands': {
            'welcome': welcome_band,
            'diagnosis': diagnosis_band,
            'options': options_band,
            'close': close_band
        },
        'critical': {
            'expectations': expectations,
            'options': options,
            'membership': membership,
            'close': close
        },
        'outcome': outcome
    }

def generate_tip(analysis, outcome, rep_name):
    """Generate coaching tip based on analysis."""
    if outcome == 'closed':
        return f"WIN — Strong presentation of options with clear pricing led to approval."
    else:
        missing = []
        if not analysis['critical']['expectations']:
            missing.append('set expectations at the start')
        if not analysis['critical']['options']:
            missing.append('present 4+ options with pricing')
        if not analysis['critical']['membership']:
            missing.append('offer the maintenance agreement')
        if not analysis['critical']['close']:
            missing.append('ask directly for the sale')

        if missing:
            return f"FIX — {missing[0].capitalize()} to increase close rate."
        else:
            return "FIX — Strengthen options presentation with more specific pricing."

def format_title_case(text):
    """Format name with title case (preserve hyphens and such)."""
    words = text.split()
    return ' '.join(w.title() for w in words)

def score_transcript(filepath):
    """Score a single transcript."""
    info, body = extract_header_and_body(filepath)
    analysis = analyze_transcript(body, info['rep'])

    # Ensure rep name is title-cased for filename
    rep_display = format_title_case(info['rep']) if info['rep'] else 'Unknown'

    card = {
        'recId': info['recId'],
        'tech': info['rep'],
        'dept': 'service',
        'jobNumber': info['job_number'] if 'job_number' in info else None,
        'dateCreated': info['date'],
        'durationMin': info['duration'],
        'bands': analysis['bands'],
        'critical': analysis['critical'],
        'outcome': analysis['outcome'],
        'tip': generate_tip(analysis, analysis['outcome'], info['rep'])
    }

    return card, info, rep_display

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 auto_score.py <transcript_path>")
        sys.exit(1)

    filepath = sys.argv[1]
    card, info, rep_display = score_transcript(filepath)
    print(json.dumps(card))
