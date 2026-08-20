"""
Simple metrics logging for PenguinCAM using SQLite.

Design philosophy:
- Fails silently if database is unavailable
- Async logging via threading (fire-and-forget)
- Minimal code overhead
- No external dependencies beyond stdlib
"""

import os
import sqlite3
import json
import tempfile
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List


# Database path from the Railway volume mount, falling back to the platform temp
# directory for local dev. Hard-coding /tmp meant every start on a Windows machine
# printed an initialization failure and silently disabled metrics - harmless on the
# deployed app, but local mode (docs/LOCAL_MODE.md) runs on exactly those machines.
DB_PATH = os.path.join(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or tempfile.gettempdir(),
                       'metrics.db')

# Global flag to track if DB is available
_db_available = True


def _init_db():
    """Initialize the metrics database with schema."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                team_number INTEGER,
                user_email TEXT,
                metadata_json TEXT
            )
        ''')

        # Create indexes for common queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_event_type
            ON events(event_type)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON events(timestamp DESC)
        ''')

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[METRICS] Failed to initialize database: {e}")
        return False


def _log_event_sync(event_type: str,
                    team_number: Optional[int] = None,
                    user_email: Optional[str] = None,
                    metadata: Optional[Dict[str, Any]] = None):
    """Synchronous event logging (called from background thread)."""
    global _db_available

    if not _db_available:
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        timestamp = datetime.utcnow().isoformat()
        metadata_json = json.dumps(metadata) if metadata else None

        cursor.execute('''
            INSERT INTO events (timestamp, event_type, team_number, user_email, metadata_json)
            VALUES (?, ?, ?, ?, ?)
        ''', (timestamp, event_type, team_number, user_email, metadata_json))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[METRICS] Failed to log event '{event_type}': {e}")
        _db_available = False


def log_event(event_type: str,
              team_number: Optional[int] = None,
              user_email: Optional[str] = None,
              metadata: Optional[Dict[str, Any]] = None):
    """
    Log an event asynchronously.

    Args:
        event_type: Type of event (e.g., 'onshape_import', 'gcode_generated')
        team_number: FRC team number if applicable
        user_email: User email if available
        metadata: Additional data to store as JSON

    Example:
        log_event('gcode_generated', team_number=6238, metadata={'material': 'aluminum'})
    """
    # Fire and forget - don't wait for database write
    thread = threading.Thread(
        target=_log_event_sync,
        args=(event_type, team_number, user_email, metadata),
        daemon=True
    )
    thread.start()


def get_summary() -> Optional[Dict[str, Any]]:
    """
    Get summary metrics.

    Returns dict with:
        - event_counts: Dict of event_type -> count
        - unique_users: Count of unique user emails
        - unique_teams: Count of unique team numbers
        - total_events: Total event count
        - date_range: First and last event timestamps
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Event counts by type
        cursor.execute('''
            SELECT event_type, COUNT(*) as count
            FROM events
            GROUP BY event_type
            ORDER BY count DESC
        ''')
        event_counts = dict(cursor.fetchall())

        # Unique users
        cursor.execute('SELECT COUNT(DISTINCT user_email) FROM events WHERE user_email IS NOT NULL')
        unique_users = cursor.fetchone()[0]

        # Unique teams
        cursor.execute('SELECT COUNT(DISTINCT team_number) FROM events WHERE team_number IS NOT NULL')
        unique_teams = cursor.fetchone()[0]

        # Total events
        cursor.execute('SELECT COUNT(*) FROM events')
        total_events = cursor.fetchone()[0]

        # Date range
        cursor.execute('SELECT MIN(timestamp), MAX(timestamp) FROM events')
        first_event, last_event = cursor.fetchone()

        conn.close()

        return {
            'event_counts': event_counts,
            'unique_users': unique_users,
            'unique_teams': unique_teams,
            'total_events': total_events,
            'date_range': {
                'first_event': first_event,
                'last_event': last_event
            }
        }
    except Exception as e:
        print(f"[METRICS] Failed to get summary: {e}")
        return None


def get_events(event_type: Optional[str] = None,
               limit: int = 100,
               offset: int = 0) -> Optional[List[Dict[str, Any]]]:
    """
    Get recent events, optionally filtered by type.

    Args:
        event_type: Filter by event type (None = all events)
        limit: Maximum number of events to return
        offset: Number of events to skip (for pagination)

    Returns list of event dicts with keys:
        - id, timestamp, event_type, team_number, user_email, metadata
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row  # Return rows as dicts
        cursor = conn.cursor()

        if event_type:
            cursor.execute('''
                SELECT id, timestamp, event_type, team_number, user_email, metadata_json
                FROM events
                WHERE event_type = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            ''', (event_type, limit, offset))
        else:
            cursor.execute('''
                SELECT id, timestamp, event_type, team_number, user_email, metadata_json
                FROM events
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset))

        rows = cursor.fetchall()
        conn.close()

        # Convert to list of dicts and parse JSON metadata
        events = []
        for row in rows:
            event = dict(row)
            if event['metadata_json']:
                event['metadata'] = json.loads(event['metadata_json'])
            else:
                event['metadata'] = None
            del event['metadata_json']
            events.append(event)

        return events
    except Exception as e:
        print(f"[METRICS] Failed to get events: {e}")
        return None


# Initialize database on module import
_db_available = _init_db()
if _db_available:
    print(f"[METRICS] Database initialized at {DB_PATH}")
else:
    print(f"[METRICS] Database unavailable - metrics will not be collected")
