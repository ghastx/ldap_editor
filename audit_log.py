import os
import sqlite3
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            contact_uid TEXT NOT NULL,
            details TEXT,
            user_ip TEXT
        )"""
    )
    conn.commit()
    return conn


def log_action(action, contact_uid, details="", user_ip=""):
    """Record an action in the audit log.

    action: 'aggiunto', 'modificato', 'eliminato'
    """
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO audit_log (timestamp, action, contact_uid, details, user_ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, contact_uid, details, user_ip),
        )
        conn.commit()
    finally:
        conn.close()


def get_log(limit=200):
    """Return the most recent audit log entries."""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
