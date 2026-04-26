#!/usr/bin/env python3
"""
Prune cruft and hallucinations from voice-memory.json.

Prunes:
- Repeated/repetitive text ("I'm going to show you how to show you how to...")
- Hallucinated phrases ("Thank you", "I love you", "uh...", "you" as standalone)
- Garbage from model timeouts ("I had trouble thinking about that one")
- Very short fragments (<3 words that aren't real questions)
- Obvious noise transcriptions

Usage:
    python3 prune-voice-memory.py [--dry-run] [--aggressive]

Rollback: cp voice-memory.json.v1.0-prod voice-memory.json
"""

import json, sys, re
from pathlib import Path

MEMORY_FILE = Path.home() / ".hermes" / "voice-memory.json"

HALLUCINATION_PATTERNS = [
    r"(?i)^thank you\.?$",
    r"(?i)^i love you\.?$",
    r"(?i)^you\.?$",
    r"(?i)^uh\.?\.?\.?$",
    r"(?i)^okay\.?$",
    r"(?i)^i'm going to show you how to show you how to",  # repetition loop
    r"(?i)^i'm going to put the lid on the lid on the lid",
    r"(?i)^i'm going to make a little bit of a bit of a bit",
    r"(?i)^i'm sorry, i'm sorry, i'm sorry",
]

MODEL_ERROR_RESPONSES = [
    "i had trouble thinking about that one",
    "i'm sorry, i couldn't hear you",
    "i'm sorry, but i can't hear you right now",
]

def is_garbage(user_text, hermes_text):
    """Check if this memory entry is garbage."""
    user = user_text.strip().lower()
    hermes = hermes_text.strip().lower()
    
    # Check user text against hallucination patterns
    for pattern in HALLUCINATION_PATTERNS:
        if re.match(pattern, user):
            return True, f"Hallucination pattern: {pattern}"
    
    # Check for repetition loops (>100 chars with heavy repetition)
    if len(user) > 100 and user.count('i\'m going to') > 3:
        return True, "Repetition loop"
    if len(user) > 100 and user.count('show you how to') > 3:
        return True, "Repetition loop"
    
    # Check for model error responses (both sides were garbage)
    if any(err in hermes for err in MODEL_ERROR_RESPONSES):
        # Only prune if user text is also clearly garbage
        if len(user.split()) < 8:
            return True, "Model error + short user text"
    
    # Hallucination patterns — only if hermes response is also short/garbage
    for pattern in HALLUCINATION_PATTERNS:
        if re.match(pattern, user):
            # Only prune if this looks like ambient noise (hermes didn't give a real answer)
            if len(hermes.split()) < 8 or any(err in hermes for err in MODEL_ERROR_RESPONSES):
                return True, f"Hallucination pattern: {pattern}"

    # Very short fragments (<2 words) that aren't real follow-ups
    if len(user.split()) < 2 and not user.endswith('?'):
        return True, "Too short fragment"
    
    return False, None

def prune_memory(dry_run=False, aggressive=False):
    mem = json.loads(MEMORY_FILE.read_text())
    original = len(mem)
    pruned = []
    kept = 0
    removed = 0
    
    for i, entry in enumerate(mem):
        garbage, reason = is_garbage(entry['user'], entry['hermes'])
        if garbage:
            print(f"  REMOVED [{i}]: {reason}")
            print(f"    User: \"{entry['user'][:80]}...\"")
            print(f"    Hermia: \"{entry['hermes'][:80]}...\"")
            removed += 1
        else:
            pruned.append(entry)
            kept += 1
    
    print(f"\nOriginal: {original} entries")
    print(f"Kept: {kept} entries")
    print(f"Removed: {removed} entries")
    
    if not dry_run and removed > 0:
        # Create backup before writing
        backup = MEMORY_FILE.with_suffix('.json.backup')
        MEMORY_FILE.rename(backup)
        print(f"\nBackup created: {backup}")
        
        MEMORY_FILE.write_text(json.dumps(pruned, indent=2))
        print(f"Pruned memory saved: {len(pruned)} entries")
    elif dry_run:
        print("\n(Dry run — no changes made)")

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    prune_memory(dry_run=dry_run)
