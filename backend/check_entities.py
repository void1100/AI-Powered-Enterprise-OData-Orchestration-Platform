import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

async def check():
    from app.services.service_manager import service_manager
    await service_manager.recover_from_graph()
    svc = service_manager._services.get('sopra-po')
    if svc:
        for e in svc.get('entity_sets', []):
            name = e.get('name', '')
            props = e.get('properties', [])
            if 'Item' in name:
                print(f"\n=== {name} ({len(props)} props) ===")
                for p in props:
                    print(f"  {p.get('name')}")
    else:
        print("No sopra-po")

asyncio.run(check())
