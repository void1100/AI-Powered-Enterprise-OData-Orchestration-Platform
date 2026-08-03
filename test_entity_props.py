from app.services.service_manager import service_manager
import sys

# Access internal services dict directly
services_dict = service_manager._services
sys.stdout.write(f'Registered services: {list(services_dict.keys())}\n')

for sid, svc in services_dict.items():
    if sid != 'pp-mpe-order':
        continue
    props = {}
    et_list = svc["metadata"].get("entity_types", [])
    for es in svc["metadata"].get("entity_sets", []):
        es_name = es["name"]
        et_name = es.get("entity_type", es_name)
        et = next((e for e in et_list if e["name"] == et_name), None)
        if not et and "." in et_name:
            local_name = et_name.rsplit(".", 1)[-1]
            et = next((e for e in et_list if e["name"] == local_name), None)
        if not et:
            et = next((e for e in et_list if et_name.endswith(e["name"])), None)
        prop_names = [p["name"] for p in (et or {}).get("properties", [])]
        props[es_name] = prop_names

    for es_name in ['C_Manageoperations', 'I_ManufacturingOrder', 'I_BillOfMaterialItemCategory']:
        p = props.get(es_name, [])
        bill = [x for x in p if 'BillOfMaterial' in x]
        order = [x for x in p if 'OrderType' in x or 'ManufacturingOrderType' in x]
        sys.stdout.write(f'{es_name}: bill={bill}, order={order}, total={len(p)}\n')

sys.stdout.flush()
