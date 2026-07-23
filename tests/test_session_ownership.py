"""Tests for session ownership — ensures users only see/modify their own sessions."""
import unittest
import os
import tempfile
import sys

# Point to backend app directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Override DB path to a temp file for isolated testing
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["SQLITE_DB_PATH"] = _tmp_db.name


class TestSessionOwnership(unittest.TestCase):

    def setUp(self):
        """Re-import sqlite_store fresh for each test, using the temp DB."""
        import importlib
        import app.db.sqlite_store as store_mod
        importlib.reload(store_mod)
        self.store = store_mod
        # Reset connection so schema is re-applied to the temp DB
        store_mod._conn = None

    def tearDown(self):
        import app.db.sqlite_store as store_mod
        if store_mod._conn:
            store_mod._conn.close()
            store_mod._conn = None

    # ------------------------------------------------------------------
    # create_session — user_id is stored
    # ------------------------------------------------------------------

    def test_create_session_stores_user_id(self):
        """Sessions created with a user_id should persist it."""
        sid = self.store.create_session(title="Alice's chat", user_role="user", user_id="alice")
        session = self.store.get_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["user_id"], "alice")
        self.assertEqual(session["title"], "Alice's chat")

    def test_create_session_without_user_id(self):
        """Sessions created without a user_id should have user_id=None (legacy compat)."""
        sid = self.store.create_session(title="Anon chat")
        session = self.store.get_session(sid)
        self.assertIsNone(session["user_id"])

    # ------------------------------------------------------------------
    # list_sessions — ownership filtering
    # ------------------------------------------------------------------

    def test_list_sessions_scoped_to_user(self):
        """A regular user should only see their own sessions."""
        self.store.create_session(title="Alice 1", user_id="alice")
        self.store.create_session(title="Alice 2", user_id="alice")
        self.store.create_session(title="Bob 1", user_id="bob")

        alice_sessions = self.store.list_sessions(user_id="alice", is_admin=False)
        titles = [s["title"] for s in alice_sessions]

        self.assertIn("Alice 1", titles)
        self.assertIn("Alice 2", titles)
        self.assertNotIn("Bob 1", titles)

    def test_admin_sees_all_sessions(self):
        """Admins should see all sessions regardless of owner."""
        self.store.create_session(title="Alice 1", user_id="alice")
        self.store.create_session(title="Bob 1", user_id="bob")
        self.store.create_session(title="Legacy", user_id=None)

        all_sessions = self.store.list_sessions(user_id="admin_user", is_admin=True)
        titles = [s["title"] for s in all_sessions]

        self.assertIn("Alice 1", titles)
        self.assertIn("Bob 1", titles)
        self.assertIn("Legacy", titles)

    def test_list_sessions_no_user_id_returns_all(self):
        """Unauthenticated (user_id=None, is_admin=False) returns all for backward compat."""
        self.store.create_session(title="Alice 1", user_id="alice")
        self.store.create_session(title="Bob 1", user_id="bob")

        sessions = self.store.list_sessions(user_id=None, is_admin=False)
        titles = [s["title"] for s in sessions]

        self.assertIn("Alice 1", titles)
        self.assertIn("Bob 1", titles)

    def test_user_cannot_see_another_users_sessions(self):
        """Bob should not see Alice's sessions."""
        self.store.create_session(title="Alice only", user_id="alice")

        bob_sessions = self.store.list_sessions(user_id="bob", is_admin=False)
        titles = [s["title"] for s in bob_sessions]

        self.assertNotIn("Alice only", titles)

    # ------------------------------------------------------------------
    # get_session — direct lookup always works (ownership enforced at API level)
    # ------------------------------------------------------------------

    def test_get_session_returns_correct_session(self):
        """get_session should return the session with all fields."""
        sid = self.store.create_session(title="Test Session", user_role="analyst", user_id="charlie")
        session = self.store.get_session(sid)

        self.assertEqual(session["id"], sid)
        self.assertEqual(session["title"], "Test Session")
        self.assertEqual(session["user_role"], "analyst")
        self.assertEqual(session["user_id"], "charlie")

    def test_get_session_returns_none_for_missing(self):
        """get_session should return None for a non-existent session."""
        result = self.store.get_session("nonexistent-id-12345")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Migration — user_id column added to existing DBs
    # ------------------------------------------------------------------

    def test_migration_adds_user_id_column(self):
        """Schema init should add user_id column even to old DBs lacking it."""
        conn = self.store._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        self.assertIn("user_id", cols)

    # ------------------------------------------------------------------
    # Session isolation — messages stay within owned sessions
    # ------------------------------------------------------------------

    def test_messages_isolated_per_session(self):
        """Messages added to one session should not appear in another."""
        sid_a = self.store.create_session(title="Session A", user_id="alice")
        sid_b = self.store.create_session(title="Session B", user_id="bob")

        self.store.add_message(sid_a, "user", "Hello from Alice")
        self.store.add_message(sid_b, "user", "Hello from Bob")

        msgs_a = self.store.get_messages(sid_a)
        msgs_b = self.store.get_messages(sid_b)

        self.assertEqual(len(msgs_a), 1)
        self.assertEqual(msgs_a[0]["content"], "Hello from Alice")
        self.assertEqual(len(msgs_b), 1)
        self.assertEqual(msgs_b[0]["content"], "Hello from Bob")


if __name__ == "__main__":
    unittest.main(verbosity=2)
