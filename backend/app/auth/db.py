"""
Auth database layer - Users and roles tables in SQLite.
"""
import sqlite3
import threading
import os
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

from app.auth.password import hash_password


class AuthDB:
    def __init__(self, db_path: str = "./data/app.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_tables(self):
        with self._lock:
            conn = self._get_conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'user',
                        status TEXT NOT NULL DEFAULT 'active',
                        display_name TEXT DEFAULT '',
                        failed_attempts INTEGER DEFAULT 0,
                        locked_until TEXT DEFAULT NULL,
                        last_login TEXT DEFAULT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS roles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        display_name TEXT NOT NULL,
                        description TEXT DEFAULT '',
                        permissions TEXT NOT NULL DEFAULT '{}',
                        is_system INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        username TEXT,
                        action TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        resource_id TEXT DEFAULT '',
                        details TEXT DEFAULT '',
                        ip_address TEXT DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'success',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                    CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
                    CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
                    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
                    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
                """)
                conn.commit()
                self._seed_defaults(conn)
            finally:
                conn.close()

    def _row_to_dict(self, row) -> Dict[str, Any]:
        return dict(row) if row else None

    # --- User CRUD ---

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return self._row_to_dict(row)
        finally:
            conn.close()

    def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._row_to_dict(row)
        finally:
            conn.close()

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            return self._row_to_dict(row)
        finally:
            conn.close()

    def list_users(self, role: str = None, status: str = None, search: str = None) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            query = "SELECT * FROM users WHERE 1=1"
            params = []
            if role:
                query += " AND role = ?"
                params.append(role)
            if status:
                query += " AND status = ?"
                params.append(status)
            if search:
                query += " AND (username LIKE ? OR email LIKE ? OR display_name LIKE ?)"
                params.extend([f"%{search}%"] * 3)
            query += " ORDER BY created_at DESC"
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def create_user(self, username: str, email: str, password_hash: str, role: str = "user", display_name: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, email, password_hash, role, status, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (username, email, password_hash, role, "active", display_name or username, now, now)
            )
            conn.commit()
            return self.get_user_by_id(cursor.lastrowid)
        finally:
            conn.close()

    def update_user(self, user_id: int, **kwargs) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        allowed = {"email", "role", "status", "display_name", "password_hash", "failed_attempts", "locked_until", "last_login"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_user_by_id(user_id)
        updates["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        conn = self._get_conn()
        try:
            conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
            conn.commit()
            return self.get_user_by_id(user_id)
        finally:
            conn.close()

    def delete_user(self, user_id: int) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def count_users(self) -> Dict[str, int]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT role, COUNT(*) as cnt FROM users GROUP BY role").fetchall()
            return {r["role"]: r["cnt"] for r in rows}
        finally:
            conn.close()

    # --- Role CRUD ---

    def get_role(self, name: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM roles WHERE name = ?", (name,)).fetchone()
            return self._row_to_dict(row)
        finally:
            conn.close()

    def list_roles(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    # --- Audit Log ---

    def log_audit(self, user_id: int = None, username: str = None, action: str = "", resource: str = "", resource_id: str = "", details: str = "", ip_address: str = "", status: str = "success"):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO audit_log (user_id, username, action, resource, resource_id, details, ip_address, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, action, resource, resource_id, details, ip_address, status, now)
            )
            conn.commit()
        finally:
            conn.close()

    def get_audit_log(self, user_id: int = None, action: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            query = "SELECT * FROM audit_log WHERE 1=1"
            params = []
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            if action:
                query += " AND action = ?"
                params.append(action)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def _seed_defaults(self, conn: sqlite3.Connection):
        now = datetime.now(timezone.utc).isoformat()
        # Seed default roles
        default_roles = [
            ("super_admin", "Super Admin", "Full system access", '{"*":"*"}', 1),
            ("admin", "Admin", "User and service management", '{"users":"rwd","services":"rwd","analytics":"r","audit":"r","settings":"rw"}', 1),
            ("analyst", "Analyst", "Run queries and ML analysis", '{"queries":"r","ml":"rwd","analytics":"r"}', 1),
            ("user", "User", "Run queries and view history", '{"queries":"rw","history":"r"}', 1),
            ("viewer", "Viewer", "Read-only access", '{"queries":"r","history":"r"}', 1),
        ]
        for name, display, desc, perms, is_sys in default_roles:
            conn.execute(
                "INSERT OR IGNORE INTO roles (name, display_name, description, permissions, is_system, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, display, desc, perms, is_sys, now, now)
            )
        # Seed default super_admin user (password: admin123!)
        admin_hash = hash_password("admin123!")
        conn.execute(
            "INSERT OR IGNORE INTO users (username, email, password_hash, role, status, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("admin", "admin@localhost", admin_hash, "super_admin", "active", "Administrator", now, now)
        )
        conn.commit()


# Singleton
_auth_db: Optional[AuthDB] = None


def get_auth_db(db_path: str = "./data/app.db") -> AuthDB:
    global _auth_db
    if _auth_db is None:
        _auth_db = AuthDB(db_path)
    return _auth_db
