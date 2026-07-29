"""Tests for ConversationSummaryMemory."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.langchain_memory import ConversationSummaryMemory


class TestConversationSummaryMemory(unittest.TestCase):
    def setUp(self):
        self.memory = ConversationSummaryMemory(keep_recent=4)

    def test_empty_messages(self):
        ctx = self.memory.build_context([], {})
        self.assertEqual(ctx["summary"], "")
        self.assertNotIn("recent_turns", ctx)
        self.assertEqual(ctx["recent_turns_langchain"], [])

    def test_short_conversation_no_summary(self):
        messages = [
            {"role": "user", "content": "Show products"},
            {"role": "assistant", "content": "Here are 77 products."},
        ]
        ctx = self.memory.build_context(messages, {"last_entity_set": "Products"})
        self.assertEqual(ctx["summary"], "")
        self.assertEqual(len(ctx["recent_turns"]), 2)
        self.assertEqual(ctx["last_entity_set"], "Products")

    def test_long_conversation_sums_old_turns(self):
        messages = [
            {"role": "user", "content": "Show customers"},
            {"role": "assistant", "content": "Here are 91 customers."},
            {"role": "user", "content": "Filter by USA"},
            {"role": "assistant", "content": "Filtered to 13 USA customers."},
            {"role": "user", "content": "Show products"},
            {"role": "assistant", "content": "Here are 77 products."},
            {"role": "user", "content": "How many?"},
        ]
        ctx = self.memory.build_context(messages, {"last_entity_set": "Products"})
        self.assertIn("customers", ctx["summary"].lower())
        self.assertEqual(len(ctx["recent_turns"]), 4)
        # recent_turns are the last 4 messages (indices 3-6), so the first recent turn is "Filtered to 13 USA customers."
        self.assertIn("filtered", ctx["recent_turns"][0]["content"].lower())
        self.assertGreater(ctx["token_estimate"], 0)

    def test_enriched_context_preserves_existing_fields(self):
        messages = [{"role": "user", "content": "test"}]
        base_ctx = {
            "last_entity_set": "Orders",
            "last_service_id": "northwind",
            "last_filter": "Country eq 'USA'",
        }
        ctx = self.memory.build_context(messages, base_ctx)
        self.assertEqual(ctx["last_entity_set"], "Orders")
        self.assertEqual(ctx["last_service_id"], "northwind")
        self.assertEqual(ctx["last_filter"], "Country eq 'USA'")

    def test_format_for_llm_prompt(self):
        ctx = {
            "summary": "User asked about customers",
            "last_entity_set": "Customers",
            "last_service_id": "northwind",
            "recent_turns": [{"role": "user", "content": "Show products"}],
        }
        prompt = self.memory.format_for_llm_prompt(ctx)
        self.assertIn("Conversation summary", prompt)
        self.assertIn("entity=Customers", prompt)
        self.assertIn("Show products", prompt)

    def test_system_role_ignored(self):
        messages = [
            {"role": "system", "content": "System message"},
            {"role": "user", "content": "Show products"},
        ]
        ctx = self.memory.build_context(messages, {})
        self.assertEqual(len(ctx["recent_turns"]), 1)

    def test_empty_content_ignored(self):
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Hello"},
        ]
        ctx = self.memory.build_context(messages, {})
        self.assertEqual(len(ctx["recent_turns"]), 1)

    def test_keep_recent_boundary(self):
        """Exactly keep_recent messages = all kept, no summary."""
        messages = [
            {"role": "user", "content": f"Message {i}"} for i in range(4)
        ]
        ctx = self.memory.build_context(messages, {})
        self.assertEqual(ctx["summary"], "")
        self.assertEqual(len(ctx["recent_turns"]), 4)

    def test_one_more_than_keep_recent(self):
        """One over keep_recent triggers summary of oldest turn."""
        messages = [
            {"role": "user", "content": "Show customers"},
            {"role": "assistant", "content": "Here are 91."},
            {"role": "user", "content": "Filter by USA"},
            {"role": "assistant", "content": "Done."},
            {"role": "user", "content": "Show products"},
        ]
        ctx = self.memory.build_context(messages, {})
        self.assertEqual(len(ctx["recent_turns"]), 4)
        self.assertNotEqual(ctx["summary"], "")
        self.assertIn("Show customers", ctx["summary"])


if __name__ == "__main__":
    unittest.main()
