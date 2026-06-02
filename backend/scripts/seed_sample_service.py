"""Script to seed the system with OData services.

By default, registers the public Northwind OData v4 service. If running
inside the docker-compose stack, also registers the local sample-odata
service.

Usage:
    python -m scripts.seed_sample_service

Environment variables:
    NORTHWIND_URL          Override the Northwind URL
    SEED_LOCAL_SAMPLE=1    Also register the local sample OData service
    SAMPLE_ODATA_URL       URL of the local sample OData service
"""
import asyncio
import os

import httpx

from app.services.service_manager import service_manager


DEFAULT_NORTHWIND_URL = "https://services.odata.org/V4/Northwind/Northwind.svc"
DEFAULT_SAMPLE_URL = "http://sample-odata:5000"


async def check_reachable(base_url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{base_url}/$metadata", headers={"Accept": "application/xml"})
            if r.status_code == 200:
                return True
            print(f"  ! HTTP {r.status_code} from $metadata")
            return False
    except Exception as e:
        print(f"  ! Reachability check failed: {e}")
        return False


async def main():
    base_url = os.environ.get("NORTHWIND_URL", DEFAULT_NORTHWIND_URL)
    print(f"Probing Northwind OData service at {base_url} ...")
    if not await check_reachable(base_url):
        print("  Public Northwind service is not reachable from this machine.")
        print("  The seed will still try to register it; the service will be")
        print("  marked as available once it becomes reachable again.")
    print()

    services = [
        {
            "id": "northwind",
            "name": "Northwind OData",
            "base_url": base_url,
            "description": "Northwind sample OData v4 service. Contains Customers, Orders, Order_Details, Products, Categories, Suppliers, Employees, Shippers, Regions, Territories.",
        },
    ]

    if os.environ.get("SEED_LOCAL_SAMPLE", "1") not in ("0", "false", "False"):
        sample_url = os.environ.get("SAMPLE_ODATA_URL", DEFAULT_SAMPLE_URL)
        print(f"Probing local sample OData service at {sample_url} ...")
        if await check_reachable(sample_url):
            services.append({
                "id": "sample",
                "name": "Local Sample OData",
                "base_url": sample_url,
                "description": "Local sample OData v4 service with Customers, Orders, and Products.",
            })

    for s in services:
        print(f"Registering {s['name']} at {s['base_url']} ...")
        try:
            await service_manager.register_service(
                service_id=s["id"],
                name=s["name"],
                base_url=s["base_url"],
                description=s.get("description", ""),
            )
            print(f"  OK")
        except Exception as e:
            print(f"  FAILED: {e}")
    print()
    print("Done. Registered services:")
    for s in service_manager.list_services():
        print(f"  - {s['name']} ({s['id']}): {len(s['entity_sets'])} entity sets")
        for es in s["entity_sets"]:
            print(f"      * {es}")


if __name__ == "__main__":
    asyncio.run(main())
