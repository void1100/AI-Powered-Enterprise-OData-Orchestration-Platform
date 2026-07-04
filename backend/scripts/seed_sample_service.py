"""Script to seed the system with OData services.

No default services are auto-registered.
Services are registered via the Admin Portal or API.

Usage:
    python -m scripts.seed_sample_service
"""
import asyncio

from app.services.service_manager import service_manager


async def main():
    print("No default services to seed.")
    print("Use the Admin Portal to register services.")
    print()
    print("Current services:")
    for s in service_manager.list_services():
        print(f"  - {s['name']} ({s['id']}): {len(s['entity_sets'])} entity sets")


if __name__ == "__main__":
    asyncio.run(main())
