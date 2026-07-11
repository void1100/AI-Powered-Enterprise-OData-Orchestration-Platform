import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.query_optimizer import QueryIntent, QueryOptimizer


class QueryOptimizerTests(unittest.TestCase):
    def test_classifies_core_intents(self):
        optimizer = QueryOptimizer()

        self.assertEqual(optimizer.classify_intent("show products"), QueryIntent.READ)
        self.assertEqual(optimizer.classify_intent("count orders"), QueryIntent.COUNT)
        self.assertEqual(optimizer.classify_intent("predict discontinued products"), QueryIntent.PREDICT)
        self.assertEqual(optimizer.classify_intent("create a new product"), QueryIntent.CREATE)
        self.assertEqual(optimizer.classify_intent("delete product 5"), QueryIntent.DELETE_INTENT)

    def test_cache_returns_plan_within_ttl(self):
        optimizer = QueryOptimizer(cache_ttl=60)
        plan = {"intent": "read", "steps": [{"service_id": "northwind", "entity_set": "Products"}]}

        optimizer.cache_plan("show products", ["northwind"], plan)

        self.assertEqual(optimizer.get_cached_plan("show products", ["northwind"]), plan)
        self.assertEqual(optimizer.stats["cache_hits"], 1)

    def test_smart_select_includes_id_columns(self):
        optimizer = QueryOptimizer()
        selected = optimizer.compute_smart_select(
            "show product names and prices",
            ["ProductID", "ProductName", "UnitPrice", "UnitsInStock"],
        )

        self.assertIn("ProductID", selected)
        self.assertIn("ProductName", selected)
        self.assertIn("UnitPrice", selected)


if __name__ == "__main__":
    unittest.main()
