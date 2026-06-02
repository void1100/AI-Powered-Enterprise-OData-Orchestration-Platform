"""End-to-end smoke test against the live Northwind OData service.

Usage:
    python -m scripts.smoke_test_northwind

This script:
  1. Registers the public Northwind OData service.
  2. Runs a handful of natural-language queries through the orchestrator.
  3. Prints the resulting plan, table, and OData URL for each.
"""
import asyncio
import json
from typing import Any, Dict

from app.services.service_manager import service_manager
from app.agents.orchestrator import orchestrator
from app.agents.policy_engine import policy_engine


SAMPLE_QUERIES = [
    "Show top 5 customers from Germany",
    "List all products in Beverages category",
    "Show top 10 orders with status Shipped",
    "How many customers are in France?",
    "Show top 5 most expensive products",
    "Show customers with their orders",
]


async def main():
    policy_engine.ensure_default_roles()
    print("Registering Northwind OData service ...")
    svc = await service_manager.register_service(
        service_id="northwind",
        name="Northwind OData",
        base_url="https://services.odata.org/V4/Northwind/Northwind.svc",
        description="Public Northwind OData v4 service.",
    )
    print(f"  Found {len(svc['metadata'].get('entity_sets', []))} entity sets")
    print()
    for q in SAMPLE_QUERIES:
        print(f"Q: {q}")
        result = await orchestrator.run(user_query=q, session_id=None, user_role="Admin")
        print(f"  Intent: {result.get('plan', {}).get('intent')}")
        print(f"  Summary: {result.get('summary')}")
        if result.get("table"):
            t = result["table"]
            print(f"  Rows: {t['row_count']} (showing {len(t['rows'])})")
            if t.get("rows"):
                print(f"  First row: {json.dumps(t['rows'][0], default=str)[:200]}")
        if result.get("primary_url"):
            print(f"  URL: {result['primary_url']}")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
