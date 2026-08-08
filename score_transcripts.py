#!/usr/bin/env python3
"""Score SERVICE call transcripts according to live-coach rubric."""
import json
import re
import os
from pathlib import Path
from datetime import datetime

def parse_transcript_file(filepath):
    """Parse transcript and extract headers and dialogue."""
    with open(filepath, 'r') as f:
        lines = f.readlines()

    # Parse headers
    rec_id = None
    rep_name = None
    date_created = None
    duration_str = None
    job_number = None

    body_start = 0
    for i, line in enumerate(lines):
        line = line.rstrip('\n')
        if line.startswith('RecId:'):
            rec_id = line.replace('RecId:', '').strip()
        elif line.startswith('Rep:'):
            rep_name = line.replace('Rep:', '').strip()
        elif line.startswith('Date:'):
            date_created = line.replace('Date:', '').strip()
        elif line.startswith('Duration:'):
            duration_str = line.replace('Duration:', '').strip()
        elif line.startswith('Job:'):
            job_line = line.replace('Job:', '').strip()
            # Extract job number
            match = re.search(r'#\s*(\d+)', job_line)
            if match:
                job_number = match.group(1)
        elif '=' * 10 in line:
            body_start = i + 1
            break

    # Generate pseudo-id if missing
    if not rec_id or rec_id == '':
        filename = Path(filepath).stem
        pseudo_id = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
        pseudo_id = re.sub(r'_+', '_', pseudo_id)
        rec_id = f"noid_{pseudo_id}"

    # Extract duration as int
    duration_match = re.search(r'(\d+)', duration_str or '')
    duration = int(duration_match.group(1)) if duration_match else 0

    # Build dialogue from body
    body_lines = [l.rstrip('\n').strip() for l in lines[body_start:] if l.strip()]

    # Reflow one-word-per-line into speaker turns
    turns = []
    current_speaker = None
    current_words = []

    for line in body_lines:
        if line.endswith(':') and len(line.split()) == 1:
            # Speaker label
            if current_speaker and current_words:
                text = ' '.join(current_words)
                turns.append((current_speaker, text))
            current_speaker = line.rstrip(':')
            current_words = []
        else:
            # Word/phrase
            if current_speaker:
                current_words.append(line)

    # Final turn
    if current_speaker and current_words:
        turns.append((current_speaker, ' '.join(current_words)))

    return {
        'rec_id': rec_id,
        'rep_name': rep_name,
        'date_created': date_created,
        'job_number': job_number,
        'duration': duration,
        'turns': turns,
        'filepath': filepath
    }

def analyze_transcript(data):
    """Analyze transcript and score."""
    turns = data['turns']
    full_text = ' '.join([t[1].lower() for t in turns])

    # Calculate scores for each band
    welcome = score_welcome(turns, full_text)
    diagnosis = score_diagnosis(turns, full_text)
    options = score_options(turns, full_text)
    close = score_close(turns, full_text)

    # Critical actions
    expectations = check_expectations(turns, full_text)
    options_count = check_options_count(turns, full_text)
    membership = check_membership(full_text)
    close_asked = check_close_asked(turns, full_text)

    # Outcome
    outcome = determine_outcome(turns, full_text)

    # Coaching tip
    tip = generate_tip(outcome, welcome, diagnosis, options, close, expectations, options_count, membership, close_asked)

    return {
        'bands': {'welcome': welcome, 'diagnosis': diagnosis, 'options': options, 'close': close},
        'critical': {'expectations': expectations, 'options': options_count, 'membership': membership, 'close': close_asked},
        'outcome': outcome,
        'tip': tip
    }

def score_welcome(turns, full_text):
    """Score greeting, professionalism, setting expectations."""
    first_turns = ' '.join([t[1].lower() for t in turns[:5]])

    greetings = any(w in first_turns for w in ['hello', 'hi', 'good', 'wonderful to meet'])
    tech_intro = any(w in first_turns for w in ['i\'m', 'name is', 'my name'])
    expectations = any(w in first_turns for w in ['going to', 'will', 'evaluation', 'check', 'look', 'inspect'])

    if greetings and tech_intro and expectations:
        return "Strong"
    elif (greetings and tech_intro) or (expectations and tech_intro):
        return "Solid"
    elif greetings or tech_intro:
        return "Moderate"
    else:
        return "Weak"

def score_diagnosis(turns, full_text):
    """Score thorough assessment, checking, explaining."""
    # Count inspection activities
    inspect_words = ['check', 'test', 'look', 'inspect', 'see', 'thermostat', 'filter', 'unit']
    inspect_count = sum(full_text.count(w) for w in inspect_words)

    # Count explanations (why/how language)
    explain_words = ['because', 'what', 'how', 'works', 'means', 'that\'s']
    explain_count = sum(full_text.count(w) for w in explain_words)

    # Count questions to customer
    questions = full_text.count('?')

    total = inspect_count + explain_count // 2 + questions // 2

    if total >= 20:
        return "Strong"
    elif total >= 12:
        return "Solid"
    elif total >= 5:
        return "Moderate"
    else:
        return "Weak"

def score_options(turns, full_text):
    """Score presenting good/better/best options with pricing."""
    # Look for prices
    prices = re.findall(r'\$\s*\d+|\d+\s*(?:dollar|grand)', full_text)

    # Look for option language
    option_words = ['option', 'upgrade', 'better', 'best', 'or', 'also', 'instead', 'another']
    option_count = sum(full_text.count(w) for w in option_words)

    if len(prices) >= 2 and option_count >= 5:
        return "Strong"
    elif len(prices) >= 1 and option_count >= 3:
        return "Solid"
    elif len(prices) >= 1 or option_count >= 2:
        return "Moderate"
    else:
        return "Weak"

def score_close(turns, full_text):
    """Score direct ask for sale, handling objections, confirming next steps."""
    # Direct close language
    close_words = ['approve', 'go ahead', 'can we', 'would you like', 'ready', 'schedule', 'book', 'let\'s do']
    close_count = sum(full_text.count(w) for w in close_words)

    # Objection handling
    has_objection_handling = 'but' in full_text or 'however' in full_text

    # Confirming next steps
    has_confirmation = 'confirm' in full_text or 'so' in full_text

    if close_count >= 2 and (has_objection_handling or has_confirmation):
        return "Strong"
    elif close_count >= 2:
        return "Solid"
    elif close_count >= 1:
        return "Moderate"
    else:
        return "Weak"

def check_expectations(turns, full_text):
    """Check if clear expectations were set at start."""
    first_turns = ' '.join([t[1].lower() for t in turns[:10]])
    expect_words = ['going to', 'will', 'cost', 'how long', 'thorough', 'evaluation', 'check']
    return sum(first_turns.count(w) for w in expect_words) >= 1

def check_options_count(turns, full_text):
    """Check if 4+ options presented (proxy: at least 2 price points)."""
    prices = len(re.findall(r'\$\s*\d+|\d+\s*(?:dollar|grand)', full_text))
    return prices >= 2

def check_membership(full_text):
    """Check if membership/SAM agreement offered."""
    membership_words = ['membership', 'sam', 'agreement', 'plan', 'service agreement', 'maintenance plan', 'contract']
    return any(w in full_text for w in membership_words)

def check_close_asked(turns, full_text):
    """Check if direct close attempted."""
    close_phrases = ['go ahead', 'approve', 'ready', 'would you like', 'can we']
    return any(p in full_text for p in close_phrases)

def determine_outcome(turns, full_text):
    """Determine closed, no-close, or unknown."""
    if not turns:
        return "unknown"

    # Check last few turns for outcome signals
    last_text = ' '.join([t[1].lower() for t in turns[-3:]])

    # Approval signals
    approvals = ['yes', 'okay', 'sounds good', 'go ahead', 'approve', 'agree', 'let\'s']
    approval_count = sum(last_text.count(w) for w in approvals)

    # Rejection/deferment signals
    deferrals = ['think about', 'let me think', 'maybe', 'later', 'call you back', 'i\'ll get back']
    deferral_count = sum(last_text.count(w) for w in deferrals)

    # Call cut-off signals
    cutoff_words = ['thank you', 'appreciate', 'goodbye', 'bye']
    is_cutoff = any(w in last_text for w in cutoff_words)

    if deferral_count > 0 or (is_cutoff and approval_count == 0):
        return "unknown"
    elif approval_count > 0:
        return "closed"
    elif deferral_count > approval_count:
        return "no-close"
    else:
        return "unknown"

def generate_tip(outcome, welcome, diagnosis, options, close, expectations, options_count, membership, close_asked):
    """Generate coaching tip."""
    if outcome == "closed":
        if close in ["Strong", "Strong on wins"]:
            return "WIN — Direct ask for the sale closed the deal."
        elif diagnosis in ["Strong"]:
            return "WIN — Thorough diagnosis built trust that led to approval."
        elif welcome in ["Strong"]:
            return "WIN — Strong opening set the tone for the entire interaction."
        else:
            return "WIN — Customer approved the work."
    else:
        if not close_asked:
            return "FIX — Ask directly for the sale: 'Would you like to go ahead with that?'"
        elif not options_count:
            return "FIX — Present at least 4 distinct options with specific pricing for each."
        elif not membership:
            return "FIX — Offer the membership/SAM agreement to protect against future issues."
        elif diagnosis in ["Weak", "Moderate"]:
            return "FIX — Spend more time explaining what you found so they understand the value."
        else:
            return "FIX — After presenting options, directly ask for approval."

def write_output_files(data, analysis, rec_id, rep_name, date_created, job_number, duration):
    """Write compact and full scorecard files."""
    # Compact card (JSON)
    compact = {
        "recId": rec_id,
        "tech": rep_name,
        "dept": "service",
        "jobNumber": job_number,
        "dateCreated": date_created,
        "durationMin": duration,
        "bands": analysis['bands'],
        "critical": analysis['critical'],
        "outcome": analysis['outcome'],
        "tip": analysis['tip']
    }

    compact_path = f"/home/user/sierra-ropp-hourly/livecards_service/{rec_id}.card.json"
    os.makedirs(os.path.dirname(compact_path), exist_ok=True)
    with open(compact_path, 'w') as f:
        f.write(json.dumps(compact) + '\n')

    # Full card (markdown)
    rep_clean = rep_name.replace(' ', '_') if rep_name else "Unknown"
    date_part = date_created[:10] if date_created else "unknown"

    full_path = f"/home/user/sierra-ropp-hourly/scorecards_full/{date_part}/svc__{rec_id}__{rep_clean}.md"

    critical = analysis['critical']
    full_content = f"""# Service Call Scorecard

**Rep:** {rep_name}
**Date:** {date_created}
**Duration:** {duration} min
**Job Number:** {job_number or 'N/A'}
**RecId:** {rec_id}

## Performance Bands

- **Welcome:** {analysis['bands']['welcome']}
- **Diagnosis:** {analysis['bands']['diagnosis']}
- **Options:** {analysis['bands']['options']}
- **Close:** {analysis['bands']['close']}

## Critical Actions

- Expectations set: {'✓' if critical['expectations'] else '✗'}
- Options presented (4+): {'✓' if critical['options'] else '✗'}
- Membership offered: {'✓' if critical['membership'] else '✗'}
- Direct close asked: {'✓' if critical['close'] else '✗'}

## Outcome

**{analysis['outcome'].upper()}**

## Coaching Tip

{analysis['tip']}
"""

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w') as f:
        f.write(full_content)

    return compact, compact_path

def process_transcript(filepath):
    """Process a single transcript file."""
    try:
        data = parse_transcript_file(filepath)
        analysis = analyze_transcript(data)
        compact, path = write_output_files(
            data, analysis,
            data['rec_id'], data['rep_name'],
            data['date_created'], data['job_number'],
            data['duration']
        )
        return compact
    except Exception as e:
        print(f"Error processing {filepath}: {e}")
        return None

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python score_transcripts.py <transcript_file>")
        sys.exit(1)

    result = process_transcript(sys.argv[1])
    if result:
        print(json.dumps(result))
