import requests, json, time

# Direct OData query to check the entity
resp = requests.post('http://localhost:8000/chat', json={
    'query': 'check C_ProductSeasonYearVH',
    'user_id': 'debug3',
    'user_role': 'Admin',
    'selected_entities': [{
        'service_id': 'pp-mpe-order',
        'entity_name': 'C_ProductSeasonYearVH',
        'properties': ['ManufacturingOrder', 'ProductSeason', 'ProductSeasonYear']
    }]
}, timeout=60)
data = resp.json()
print("Provider:", data.get('llm_provider'))
print("Summary:", data.get('summary'))
print("Rows:", data.get('table', {}).get('row_count') if data.get('table') else 0)
if data.get('table') and data['table'].get('rows'):
    print("Sample:", json.dumps(data['table']['rows'][:3], indent=2))
