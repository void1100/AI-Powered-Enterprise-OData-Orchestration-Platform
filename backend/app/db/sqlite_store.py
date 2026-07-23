"""SQLite-based storage for chat history, sessions, and run logs."""
import os
import json
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional
from loguru import logger

from app.config import settings


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(settings.sqlite_db_path) or ".", exist_ok=True)
        _conn = sqlite3.connect(settings.sqlite_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            user_role TEXT,
            user_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            plan_json TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            message_id TEXT,
            user_query TEXT,
            plan_json TEXT,
            tool_calls_json TEXT,
            response_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
        """
    )
    conn.commit()

    # --- Backward-compatible migration: add user_id column if missing ---
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(sessions)").fetchall()}
    if "user_id" not in existing_cols:
        cur.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
        conn.commit()
        logger.info("Migrated sessions table: added user_id column")


def create_session(title: str = "New Chat", user_role: str = "Admin", user_id: Optional[str] = None) -> str:
    sid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO sessions (id, title, user_role, user_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (sid, title, user_role, user_id, now, now),
        )
        conn.commit()
    return sid


def list_sessions(limit: int = 50, user_id: Optional[str] = None, is_admin: bool = False) -> List[Dict[str, Any]]:
    """
    Return sessions scoped to the requesting user.
    - Admins (is_admin=True) see ALL sessions.
    - Regular users see only their own sessions (filtered by user_id).
    - If user_id is None and not admin (unauthenticated), returns all for backward compat.
    """
    with _lock:
        conn = _get_conn()
        if is_admin or user_id is None:
            cur = conn.execute(
                "SELECT id, title, user_role, user_id, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = conn.execute(
                "SELECT id, title, user_role, user_id, created_at, updated_at FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            )
        return [dict(r) for r in cur.fetchall()]


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single session by ID."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, title, user_role, user_id, created_at, updated_at FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def rename_session(session_id: str, title: str):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, datetime.utcnow().isoformat(), session_id),
        )
        conn.commit()


def delete_session(session_id: str):
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
        conn.commit()


def touch_session(session_id: str):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), session_id),
        )
        conn.commit()


def add_message(
    session_id: str,
    role: str,
    content: str,
    plan: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> str:
    mid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, plan_json, result_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (mid, session_id, role, content, json.dumps(plan) if plan else None, json.dumps(result) if result else None, now),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        conn.commit()
    return mid


def get_messages(session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "SELECT id, role, content, plan_json, result_json, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
            (session_id, limit),
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("plan_json", "result_json"):
                if d.get(k):
                    try:
                        d[k.replace("_json", "")] = json.loads(d[k])
                    except Exception:
                        d[k.replace("_json", "")] = None
                    d.pop(k, None)
            rows.append(d)
        return rows


def add_run(
    session_id: Optional[str],
    message_id: Optional[str],
    user_query: str,
    plan: Optional[Dict[str, Any]],
    tool_calls: Optional[List[Dict[str, Any]]],
    response: Optional[Dict[str, Any]],
) -> str:
    rid = str(uuid.uuid4())
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO runs (id, session_id, message_id, user_query, plan_json, tool_calls_json, response_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                rid,
                session_id,
                message_id,
                user_query,
                json.dumps(plan) if plan else None,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(response) if response else None,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
    return rid


def get_runs(session_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        if session_id:
            cur = conn.execute(
                "SELECT * FROM runs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            )
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for k in ("plan_json", "tool_calls_json", "response_json"):
                if d.get(k):
                    try:
                        d[k.replace("_json", "")] = json.loads(d[k])
                    except Exception:
                        d[k.replace("_json", "")] = None
                    d.pop(k, None)
            rows.append(d)
        return rows
