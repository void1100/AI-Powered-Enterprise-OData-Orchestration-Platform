import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.query_intent_detector import detect_query_intent, _is_count_only, _extract_requested_columns


class QueryIntentDetectorTests(unittest.TestCase):
    def test_count_only_queries(self):
        self.assertTrue(_is_count_only("how many products are there?"))
        self.assertTrue(_is_count_only("count of products"))
        self.assertTrue(_is_count_only("what is the total number of orders"))
        self.assertTrue(_is_count_only("give me the count of customers"))

        # Negative count cases (grouped counts)
        self.assertFalse(_is_count_only("count of orders per customer"))
        self.assertFalse(_is_count_only("count each product by category"))
        self.assertFalse(_is_count_only("how many orders by region"))

    def test_column_select_queries(self):
        available_cols = ["ProductID", "ProductName", "UnitPrice", "UnitsInStock", "CompanyName", "Country"]
        
        # Test "show me only ProductName"
        res1 = _extract_requested_columns("show me only the ProductName of all products", available_cols)
        self.assertEqual(res1, ["ProductName"])

        # Test "give me CompanyName and Country"
        res2 = _extract_requested_columns("give me just CompanyName and Country from Customers", available_cols)
        self.assertEqual(res2, ["CompanyName", "Country"])

        # Test normal query
        res3 = _extract_requested_columns("show all products", available_cols)
        self.assertIsNone(res3)

    def test_detect_query_intent_wrapper(self):
        available_cols = ["ProductID", "ProductName", "UnitPrice"]
        
        intent1 = detect_query_intent("How many products are there?", available_cols)
        self.assertEqual(intent1["type"], "count_total")

        intent2 = detect_query_intent("Show me only the ProductName", available_cols)
        self.assertEqual(intent2["type"], "column_select")
        self.assertEqual(intent2["columns"], ["ProductName"])

        intent3 = detect_query_intent("Show all products", available_cols)
        self.assertIsNone(intent3["type"])


if __name__ == "__main__":
    unittest.main()
