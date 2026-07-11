import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.guardrails import run_input_guards, run_output_guards


class GuardrailTests(unittest.TestCase):
    def test_blocks_write_without_confirmation(self):
        result = run_input_guards(
            user_role="admin",
            user_id="test-user-confirmation",
            entity_set="Products",
            operation="create",
            fields={"ProductName": "Chai"},
            required_fields=[],
            confirmed=False,
        )

        self.assertFalse(result.allow)
        self.assertEqual(result.metadata["guard"], "confirmation")

    def test_blocks_unsafe_field_value(self):
        result = run_input_guards(
            user_role="admin",
            user_id="test-user-xss",
            entity_set="Products",
            operation="create",
            fields={"ProductName": "<script>alert(1)</script>"},
            required_fields=[],
            confirmed=True,
        )

        self.assertFalse(result.allow)
        self.assertEqual(result.metadata["guard"], "field_values")

    def test_redacts_sensitive_output_fields(self):
        response = {
            "table": {
                "rows": [
                    {"username": "admin", "password": "secret", "@odata.etag": "internal"},
                ]
            }
        }

        guarded = run_output_guards(response, "read")

        self.assertEqual(guarded["table"]["rows"][0]["password"], "***REDACTED***")
        self.assertNotIn("@odata.etag", guarded["table"]["rows"][0])


if __name__ == "__main__":
    unittest.main()
