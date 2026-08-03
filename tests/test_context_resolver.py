import sys
import unittest
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.context_resolver import extract_session_context
from app.services.query_intent_detector import detect_query_intent
from app.agents.reasoning_engine import LLMReasoningEngine


class ContextResolverTests(unittest.TestCase):
    def test_extract_session_context(self):
        messages = [
            {
                "role": "user",
                "content": "Show me products",
            },
            {
                "role": "assistant",
                "content": "Showing 77 products.",
                "plan": {
                    "intent": "fetch",
                    "target_services": ["northwind"],
                    "steps": [
                        {
                            "service_id": "northwind",
                            "entity_set": "Products",
                            "select": ["ProductID", "ProductName", "UnitPrice"],
                        }
                    ],
                },
                "result": {
                    "table": {
                        "columns": ["ProductID", "ProductName", "UnitPrice", "UnitsInStock"],
                        "rows": [
                            {"ProductID": 1, "ProductName": "Chai", "UnitPrice": 18.0},
                        ],
                    }
                },
            },
        ]

        ctx = extract_session_context(messages)
        self.assertEqual(ctx["last_service_id"], "northwind")
        self.assertEqual(ctx["last_entity_set"], "Products")
        self.assertEqual(ctx["last_columns"], ["ProductID", "ProductName", "UnitPrice", "UnitsInStock"])
        self.assertEqual(len(ctx["recent_turns"]), 2)

    def test_followup_query_uses_session_columns(self):
        messages = [
            {
                "role": "user",
                "content": "Show all products",
            },
            {
                "role": "assistant",
                "content": "Done",
                "plan": {
                    "steps": [{"service_id": "northwind", "entity_set": "Products"}]
                },
                "result": {
                    "table": {
                        "columns": ["ProductID", "ProductName", "UnitPrice", "UnitsInStock"],
                        "rows": [],
                    }
                },
            },
        ]

        ctx = extract_session_context(messages)
        
        # User follow-up query: "Show me only ProductName"
        intent = detect_query_intent("Show me only ProductName", ctx["last_columns"])
        self.assertEqual(intent["type"], "column_select")
        self.assertEqual(intent["columns"], ["ProductName"])

        # User follow-up query: "How many are there?"
        intent2 = detect_query_intent("How many are there?", ctx["last_columns"])
        self.assertEqual(intent2["type"], "count_total")

    def test_followup_query_prefers_previous_entity_over_material_false_match(self):
        engine = LLMReasoningEngine()
        services = [
            {
                "id": "pp-mpe-order",
                "name": "PP MPE Order",
                "entity_sets": ["I_ManufacturingOrder", "I_Material"],
                "entity_properties": {
                    "I_ManufacturingOrder": [
                        "ID",
                        "BillOfMaterial",
                        "OrderType",
                        "OrderInternalID",
                    ],
                    "I_Material": [
                        "Material",
                        "MaterialType",
                        "MaterialGroup",
                    ],
                },
                "entity_labels": {},
                "metadata_xml": "",
            }
        ]
        session_context = {
            "last_service_id": "pp-mpe-order",
            "last_entity_set": "I_ManufacturingOrder",
            "last_columns": ["ID", "BillOfMaterial", "OrderType", "OrderInternalID"],
        }

        plan, _ = asyncio.run(
            engine.plan(
                "show order type where bill of Material =0000034",
                services,
                memory_context=[],
                session_context=session_context,
            )
        )

        self.assertEqual(plan["steps"][0]["service_id"], "pp-mpe-order")
        self.assertEqual(plan["steps"][0]["entity_set"], "I_ManufacturingOrder")
        self.assertEqual(plan["steps"][0]["select"], ["OrderType"])
        self.assertEqual(plan["steps"][0]["filter"], "BillOfMaterial eq 0000034")


if __name__ == "__main__":
    unittest.main()
