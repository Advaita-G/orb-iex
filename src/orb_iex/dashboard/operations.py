"""Read-only operational and paired-observation evidence for the existing journal."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from ..candidate_review import review
from ..shadow_analytics import paired_summary


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None


def snapshot(root):
    root = Path(root)
    now = datetime.now(timezone.utc)
    heartbeat = _read(root / 'candidate_b/health/heartbeat.json')
    age = None
    if heartbeat and heartbeat.get('updated_at'):
        age = (now - datetime.fromisoformat(heartbeat['updated_at'])).total_seconds()
    active = heartbeat and heartbeat.get('mode') == 'shadow' and age is not None and 0 <= age < 180
    if active:
        try:
            pid = int(heartbeat.get('pid') or 0)
            if pid <= 0:
                active = False
            else:
                os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, ValueError, TypeError):
            active = False
    report = _read(root / 'health/operational-review.json')
    forward = paired_summary(root)
    journal = root / 'candidate_b/shadow.sqlite'
    day = now.astimezone(ZoneInfo('America/New_York')).date()
    paired = {'status': 'INCOMPLETE', 'paired_plans': {}, 'quote_decisions': {},
              'reason': 'no forward observations yet'}
    if journal.is_file():
        with sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute("SELECT max(day) FROM diagnostics WHERE kind='ab_plans'").fetchone()
        if row and row[0]:
            day = datetime.fromisoformat(row[0]).date()
        paired = review(journal, day)
    b = forward['portfolios']['candidate_b']
    if b['status'] == 'COLLECTING':
        paired = b['decisions']
        if paired.get('day'):
            day = datetime.fromisoformat(paired['day']).date()
    return {'runtime_mode': 'SHADOW' if active else 'BLOCKED', 'paper_ready': False,
            'paper_entries_paused': (root / 'KILL').exists(),
            'collector_heartbeat': heartbeat, 'heartbeat_age_seconds': age,
            'paired_day': day.isoformat(), 'paired': paired,
            'review': report, 'hypothetical_fills': True,
            'forward': forward,
            'performance_comparison': f"UNQUALIFIED: {forward['matched_count']} matched hypothetical outcomes; no executable economic proof",
            'excluded_winners': 'UNKNOWN unless counterfactual execution can be established',
            'cost_reduction': 'Not measured; no cost-saving claim'}
