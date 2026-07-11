import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.entity_selector import classify_join, classify_property, EntitySelector


class UnavailableGraph:
    def is_available(self):
        return False


class EntitySelectorTests(unittest.TestCase):
    def test_classify_property_prioritizes_common_business_fields(self):
        self.assertEqual(classify_property("PurchaseOrder"), "Key")
        self.assertEqual(classify_property("CreatedDate"), "Date")
        self.assertEqual(classify_property("NetAmount"), "Measure")
        self.assertEqual(classify_property("OrderStatus"), "Status")
        self.assertEqual(classify_property("MaterialText"), "Description")

    def test_detect_joins_finds_exact_key_match(self):
        selector = EntitySelector()
        selector._neo4j = UnavailableGraph()
        entities = [
            {
                "service_id": "northwind",
                "entity_name": "Products",
                "properties": ["ProductID", "ProductName", "SupplierID"],
            },
            {
                "service_id": "northwind",
                "entity_name": "Order_Details",
                "properties": ["OrderID", "ProductID", "Quantity"],
            },
        ]

        joins = selector.detect_joins(entities)

        self.assertTrue(any(j["left_key"] == "ProductID" for j in joins))
        self.assertEqual(joins[0]["label"], "primary_key")

    def test_classify_join_distinguishes_match_types(self):
        self.assertEqual(
            classify_join({"left_key": "ProductID", "right_key": "ProductID", "confidence": 0.95}),
            "primary_key",
        )
        self.assertEqual(
            classify_join({"left_key": "SupplierID", "right_key": "SupplierCode", "confidence": 0.7, "match_type": "fuzzy"}),
            "fuzzy",
        )


if __name__ == "__main__":
    unittest.main()
