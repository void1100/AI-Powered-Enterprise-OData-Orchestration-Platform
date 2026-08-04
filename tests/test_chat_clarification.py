import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.clarification import (  # noqa: E402
    extract_pending_clarification,
    is_generic_scope_query,
    resolve_pending_clarification,
    should_ask_scope_clarification,
)


class ChatClarificationTests(unittest.TestCase):
    def test_extracts_last_pending_clarification(self):
        messages = [
            {"role": "user", "content": "show orders"},
            {
                "role": "assistant",
                "content": "Which one?",
                "result": {
                    "clarification": {
                        "type": "entity_choice",
                        "query": "show orders",
                        "options": [{"label": "Purchase Order", "value": "A_PurchaseOrder"}],
                    }
                },
            },
        ]

        clarification = extract_pending_clarification(messages)
        self.assertIsNotNone(clarification)
        self.assertEqual(clarification["type"], "entity_choice")

    def test_entity_choice_accepts_numeric_reply(self):
        clarification = {
            "type": "entity_choice",
            "query": "show orders",
            "options": [
                {"label": "Purchase Order", "value": "A_PurchaseOrder", "entity_set": "A_PurchaseOrder", "service_id": "po"},
                {"label": "Production Order", "value": "I_ManufacturingOrder", "entity_set": "I_ManufacturingOrder", "service_id": "mfg"},
            ],
        }

        resolved = resolve_pending_clarification(clarification, "2")
        self.assertIn("I_ManufacturingOrder", resolved["query"])
        self.assertIn("mfg", resolved["query"])

    def test_entity_choice_for_generic_query_direct_selects(self):
        clarification = {
            "type": "entity_choice",
            "query": "show data",
            "options": [
                {"label": "Product", "value": "Products", "entity_set": "Products", "service_id": "mock", "query": "show Products"},
            ],
        }

        resolved = resolve_pending_clarification(clarification, "1")
        self.assertEqual(resolved["query"], "show Products")
        self.assertTrue(resolved["direct_select"])

    def test_entity_choice_reasks_when_reply_is_still_ambiguous(self):
        clarification = {
            "type": "entity_choice",
            "query": "show orders",
            "options": [
                {"label": "Purchase Order", "value": "A_PurchaseOrder", "entity_set": "A_PurchaseOrder", "service_id": "po"},
                {"label": "Production Order", "value": "I_ManufacturingOrder", "entity_set": "I_ManufacturingOrder", "service_id": "mfg"},
            ],
        }

        resolved = resolve_pending_clarification(clarification, "not sure")
        self.assertIn("clarification", resolved)
        self.assertEqual(resolved["clarification"]["type"], "entity_choice")

    def test_query_scope_turns_free_text_into_followup_query(self):
        clarification = {
            "type": "query_scope",
            "query": "show me data",
            "options": [],
        }

        resolved = resolve_pending_clarification(clarification, "purchase orders from germany")
        self.assertEqual(resolved["query"], "show purchase orders from germany")

    def test_scope_clarification_detects_generic_queries(self):
        self.assertTrue(is_generic_scope_query("help"))
        self.assertTrue(should_ask_scope_clarification("show data", candidates=[]))
        self.assertTrue(
            should_ask_scope_clarification(
                "show data",
                candidates=[{"entity_set": "I_WBSElementBasicData"}],
            )
        )
        self.assertFalse(
            should_ask_scope_clarification(
                "show purchase orders",
                candidates=[{"entity_set": "A_PurchaseOrder"}],
            )
        )


if __name__ == "__main__":
    unittest.main()
