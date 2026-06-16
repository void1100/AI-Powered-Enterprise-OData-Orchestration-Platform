"""OData service manager.

Maintains a registry of OData services, their clients, and their metadata.
Provides discovery, indexing, and dispatch.
"""
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from app.db.neo4j_client import neo4j_client
from app.db.memory_graph import get_memory_graph
from app.db.vector_store import vector_store
from app.services.odata_client import ODataClient
from app.services.odata_request_builder import ODataRequestBuilder
from app.services.response_sanitizer import sanitize


KNOWN_RELATIONSHIPS: Dict[str, List[Dict[str, Any]]] = {
    "northwind": [
        {"from": "Customers", "to": "Orders", "rel_type": "PLACES", "cardinality": "1_to_many",
         "join_field": "CustomerID",
         "description": "Each customer can place many orders; orders are linked back via CustomerID."},
        {"from": "Orders", "to": "Customers", "rel_type": "PLACED_BY", "cardinality": "many_to_1",
         "join_field": "CustomerID",
         "description": "Each order is placed by exactly one customer (CustomerID)."},
        {"from": "Orders", "to": "Order_Details", "rel_type": "CONTAINS", "cardinality": "1_to_many",
         "join_field": "OrderID",
         "description": "Each order has one or more line items in Order_Details."},
        {"from": "Products", "to": "Order_Details", "rel_type": "INCLUDED_IN", "cardinality": "1_to_many",
         "join_field": "ProductID",
         "description": "Each product can appear in many order details."},
        {"from": "Products", "to": "Categories", "rel_type": "BELONGS_TO", "cardinality": "many_to_1",
         "join_field": "CategoryID",
         "description": "Each product belongs to exactly one category."},
        {"from": "Products", "to": "Suppliers", "rel_type": "SUPPLIED_BY", "cardinality": "many_to_1",
         "join_field": "SupplierID",
         "description": "Each product is supplied by exactly one supplier."},
        {"from": "Suppliers", "to": "Products", "rel_type": "SUPPLIES", "cardinality": "1_to_many",
         "join_field": "SupplierID",
         "description": "Each supplier provides one or more products."},
        {"from": "Categories", "to": "Products", "rel_type": "HAS", "cardinality": "1_to_many",
         "join_field": "CategoryID",
         "description": "Each category has one or more products."},
        {"from": "Employees", "to": "Orders", "rel_type": "PROCESSED", "cardinality": "1_to_many",
         "join_field": "EmployeeID",
         "description": "Each employee can process many orders."},
        {"from": "Shippers", "to": "Orders", "rel_type": "SHIPS", "cardinality": "1_to_many",
         "join_field": "ShipperID",
         "description": "Each shipper can ship many orders."},
        {"from": "Employees", "to": "Territories", "rel_type": "ASSIGNED_TO", "cardinality": "many_to_many",
         "join_field": "EmployeeTerritories",
         "description": "Employees cover one or more sales territories."},
    ],
}


class ODataServiceManager:
    def __init__(self):
        self._services: Dict[str, Dict[str, Any]] = {}
        self._clients: Dict[str, ODataClient] = {}
        self._entity_to_set: Dict[str, Dict[str, str]] = {}
        self._custom_entities: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def graph(self):
        return neo4j_client if neo4j_client.is_available() else get_memory_graph()

    async def register_service(
        self,
        service_id: str,
        name: str,
        base_url: str,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            client = ODataClient(base_url)
            try:
                meta = await client.get_metadata()
            except Exception as e:
                logger.warning(f"Failed to fetch metadata for {name} ({base_url}): {e}")
                meta = {"entity_types": [], "entity_sets": [], "associations": [], "namespace": ""}
            self._services[service_id] = {
                "id": service_id,
                "name": name,
                "base_url": base_url,
                "description": description,
                "metadata": meta,
                "extra": metadata or {},
            }
            self._clients[service_id] = client
            self._index_service_in_graph(service_id, self._services[service_id])
            self._index_service_in_vector_store(service_id, self._services[service_id])
            return self._services[service_id]

    def _index_service_in_graph(self, service_id: str, svc: Dict[str, Any]):
        g = self.graph()
        g.upsert_service({
            "id": service_id,
            "name": svc["name"],
            "base_url": svc["base_url"],
            "description": svc["description"],
            "metadata": svc.get("extra", {}),
        })
        entity_set_to_type: Dict[str, str] = {}
        for es in svc["metadata"].get("entity_sets", []):
            entity_set_to_type[es["name"]] = es.get("entity_type") or es["name"]
        self._entity_to_set[service_id] = entity_set_to_type
        for es in svc["metadata"].get("entity_sets", []):
            et_name = (es.get("entity_type") or es["name"]).split(".")[-1]
            et = next(
                (e for e in svc["metadata"].get("entity_types", [])
                 if e["name"] == et_name or f"{e['namespace']}.{e['name']}" == es.get("entity_type")),
                None,
            )
            props = et.get("properties", []) if et else []
            allowed_ops = ["select", "filter", "expand", "orderby", "top", "skip"]
            g.upsert_entity({
                "service_id": service_id,
                "name": es["name"],
                "type": et_name,
                "description": f"Entity set {es['name']} of {et_name}. {svc['description']}",
                "allowed_ops": allowed_ops,
                "properties": [p["name"] for p in props],
            })
        for assoc in svc["metadata"].get("associations", []):
            for from_role, to_role in [("end1", "end2"), ("end2", "end1")]:
                from_type = assoc[from_role]["type"]
                to_type = assoc[to_role]["type"]
                from_set = next((es for es in svc["metadata"]["entity_sets"] if es.get("entity_type") == from_type), None)
                to_set = next((es for es in svc["metadata"]["entity_sets"] if es.get("entity_type") == to_type), None)
                if from_set and to_set:
                    g.upsert_relationship({
                        "from_service": service_id,
                        "from_name": from_set["name"],
                        "to_service": service_id,
                        "to_name": to_set["name"],
                        "rel_type": assoc.get("name", "ASSOCIATED_WITH"),
                        "cardinality": f'{assoc[from_role]["multiplicity"]}_to_{assoc[to_role]["multiplicity"]}',
                        "description": f"{from_set['name']} relates to {to_set['name']} via {assoc.get('name')}",
                    })
        for rel in KNOWN_RELATIONSHIPS.get(service_id, []):
            if rel["from"] in entity_set_to_type and rel["to"] in entity_set_to_type:
                g.upsert_relationship({
                    "from_service": service_id,
                    "from_name": rel["from"],
                    "to_service": service_id,
                    "to_name": rel["to"],
                    "rel_type": rel["rel_type"],
                    "cardinality": rel["cardinality"],
                    "description": rel["description"],
                })

    def _index_service_in_vector_store(self, service_id: str, svc: Dict[str, Any]):
        items: List[Dict[str, Any]] = []
        for es in svc["metadata"].get("entity_sets", []):
            et_name = (es.get("entity_type") or es["name"]).split(".")[-1]
            et = next(
                (e for e in svc["metadata"].get("entity_types", [])
                 if e["name"] == et_name or f"{e['namespace']}.{e['name']}" == es.get("entity_type")),
                None,
            )
            prop_names = [p["name"] for p in (et or {}).get("properties", [])]
            text = (
                f"Service: {svc['name']}. Entity set: {es['name']}. "
                f"Description: {svc['description']}. "
                f"Columns: {', '.join(prop_names)}."
            )
            items.append({
                "id": f"{service_id}::{es['name']}",
                "text": text,
                "metadata": {
                    "service_id": service_id,
                    "service_name": svc["name"],
                    "entity_set": es["name"],
                    "entity_type": et_name,
                    "properties": prop_names,
                },
            })
        for rel in KNOWN_RELATIONSHIPS.get(service_id, []):
            text = (
                f"Relationship in {svc['name']}: {rel['from']} {rel['rel_type']} {rel['to']}. "
                f"Cardinality: {rel['cardinality']}. {rel['description']}"
            )
            items.append({
                "id": f"{service_id}::rel::{rel['from']}->{rel['to']}",
                "text": text,
                "metadata": {
                    "service_id": service_id,
                    "service_name": svc["name"],
                    "from_entity": rel["from"],
                    "to_entity": rel["to"],
                    "rel_type": rel["rel_type"],
                },
            })
        if items:
            vector_store.index_tools_bulk(items)

    async def refresh_service(self, service_id: str) -> Optional[Dict[str, Any]]:
        if service_id not in self._services:
            return None
        svc = self._services[service_id]
        client = self._clients[service_id]
        try:
            meta = await client.get_metadata(force_refresh=True)
        except Exception as e:
            logger.warning(f"Refresh failed for {service_id}: {e}")
            return svc
        svc["metadata"] = meta
        self._index_service_in_graph(service_id, svc)
        self._index_service_in_vector_store(service_id, svc)
        return svc

    def list_services(self) -> List[Dict[str, Any]]:
        out = []
        for sid, svc in self._services.items():
            entity_props = {}
            for es in svc["metadata"].get("entity_sets", []):
                es_name = es["name"]
                et_name = es.get("entity_type", es_name)
                et = next((e for e in svc["metadata"].get("entity_types", []) if e["name"] == et_name), None)
                props = [p["name"] for p in (et or {}).get("properties", [])]
                entity_props[es_name] = props
            out.append({
                "id": sid,
                "name": svc["name"],
                "base_url": svc["base_url"],
                "description": svc["description"],
                "entity_sets": [es["name"] for es in svc["metadata"].get("entity_sets", [])],
                "entity_properties": entity_props,
            })
        return out

    async def recover_from_graph(self):
        """Restore service registrations from the graph DB and refresh
        metadata from the upstream OData endpoint. Used at backend startup
        so the in-memory service map stays consistent across restarts.
        Also restores custom entities and re-registers MCP tools.
        """
        g = self.graph()
        try:
            persisted = g.list_all_services()
        except Exception as e:
            logger.warning(f"Could not read services from graph: {e}")
            return
        for svc in persisted:
            sid = svc.get("id")
            base_url = svc.get("base_url")
            name = svc.get("name")
            description = svc.get("description", "")
            if not sid or not base_url:
                continue
            if sid in self._services:
                continue
            try:
                logger.info(f"Recovering service {sid} from graph ...")
                await self.register_service(
                    service_id=sid,
                    name=name or sid,
                    base_url=base_url,
                    description=description,
                )
                logger.info(f"  {sid}: recovered")
            except Exception as e:
                logger.warning(f"  Failed to recover {sid}: {e}")
        self._recover_custom_entities(g)

    def _recover_custom_entities(self, g):
        """Restore custom entities from Neo4j and re-register MCP tools."""
        try:
            custom_entities = g.get_custom_entities()
        except Exception as e:
            logger.warning(f"Could not read custom entities from graph: {e}")
            return
        for ce in custom_entities:
            sid = ce.get("service_id")
            name = ce.get("name")
            if not sid or not name:
                continue
            if sid not in self._services:
                continue
            if sid not in self._custom_entities:
                self._custom_entities[sid] = {}
            self._custom_entities[sid][name] = {
                "name": name,
                "service_id": sid,
                "base_entity_set": ce.get("base_entity_set", ""),
                "description": ce.get("description", ""),
                "default_filter": ce.get("default_filter", ""),
                "allowed_columns": ce.get("allowed_columns", []),
                "created_by": ce.get("created_by", ""),
                "created_at": ce.get("created_at", ""),
                "is_custom": True,
            }
            meta = self._services[sid]["metadata"]
            meta.setdefault("entity_sets", []).append({"name": name, "entity_type": name})
            logger.info(f"  Recovered custom entity '{name}' on {sid}")
            try:
                from app.mcp.mcp_server import mcp_server
                mcp_server.register_custom_entity_tool(
                    sid, name,
                    ce.get("description", ""),
                    ce.get("allowed_columns", []),
                    ce.get("base_entity_set", ""),
                )
            except Exception as e:
                logger.warning(f"  Failed to register MCP tool for {name}: {e}")

    def get_service(self, service_id: str) -> Optional[Dict[str, Any]]:
        return self._services.get(service_id)

    def get_client(self, service_id: str) -> Optional[ODataClient]:
        return self._clients.get(service_id)

    async def execute_plan(
        self,
        service_id: str,
        plan: Dict[str, Any],
        allowed_ops: Optional[list] = None,
        max_rows: int = 200,
    ) -> Dict[str, Any]:
        client = self.get_client(service_id)
        if not client:
            raise ValueError(f"Unknown service: {service_id}")
        builder = ODataRequestBuilder(client, allowed_ops=allowed_ops, custom_entities=self._custom_entities.get(service_id, {}))
        execution = await builder.execute(plan)
        raw = execution["result"]
        rows = raw.get("value", []) if isinstance(raw, dict) else []
        total_count = raw.get("@odata.count") if isinstance(raw, dict) else None
        url = execution["url"]

        base_url = url.split("?")[0] if "?" in url else url

        if total_count and total_count > len(rows) and total_count <= max_rows:
            page_size = len(rows) if len(rows) > 0 else 20
            skip = len(rows)
            while skip < total_count and skip < max_rows:
                try:
                    page_size_actual = min(page_size, max_rows - skip)
                    page_url = f"{base_url}?$skip={skip}&$top={page_size_actual}"
                    if "$count" in url:
                        page_url += "&$count=true"
                    client_obj = await client._get_client()
                    resp = await client_obj.get(page_url, headers={"Accept": "application/json"})
                    resp.raise_for_status()
                    page_data = resp.json()
                    page_rows = page_data.get("value", [])
                    if not page_rows:
                        break
                    rows.extend(page_rows)
                    skip += len(page_rows)
                except Exception:
                    break

        if total_count and len(rows) > max_rows:
            rows = rows[:max_rows]

        cleaned_rows = []
        for r in rows:
            if isinstance(r, dict):
                cleaned_rows.append({k: v for k, v in r.items() if k != "@odata.etag"})

        columns = []
        for r in cleaned_rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in columns and not k.startswith("@odata"):
                        columns.append(k)
        if len(columns) > 10:
            columns = columns[:10]
        cleaned_rows = [{k: v for k, v in r.items() if k in columns} for r in cleaned_rows]

        sanitized = {
            "columns": columns,
            "rows": cleaned_rows,
            "row_count": total_count or len(cleaned_rows),
            "truncated": (total_count or len(cleaned_rows)) > len(cleaned_rows),
            "total_count": total_count,
        }
        return {
            "service_id": service_id,
            "url": url,
            "table": sanitized,
        }

    # --- Custom Entity Management ---

    def register_custom_entity(
        self,
        service_id: str,
        name: str,
        base_entity_set: str,
        description: str = "",
        default_filter: str = "",
        allowed_columns: Optional[List[str]] = None,
        created_by: str = "admin",
    ) -> Dict[str, Any]:
        if service_id not in self._services:
            raise ValueError(f"Unknown service: {service_id}")
        if service_id not in self._custom_entities:
            self._custom_entities[service_id] = {}
        custom_def = {
            "name": name,
            "service_id": service_id,
            "base_entity_set": base_entity_set,
            "description": description,
            "default_filter": default_filter,
            "allowed_columns": allowed_columns or [],
            "created_by": created_by,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "is_custom": True,
        }
        self._custom_entities[service_id][name] = custom_def
        g = self.graph()
        g.upsert_entity({
            "service_id": service_id,
            "name": name,
            "type": "CustomEntity",
            "description": f"[Custom] {description}. Derived from {base_entity_set}.",
            "allowed_ops": ["select", "filter", "expand", "orderby", "top", "skip"],
            "properties": allowed_columns or [],
            "is_custom": True,
            "base_entity_set": base_entity_set,
            "default_filter": default_filter,
            "allowed_columns": allowed_columns or [],
            "created_by": created_by,
            "created_at": custom_def["created_at"],
        })
        svc = self._services[service_id]
        svc["metadata"].setdefault("entity_sets", []).append({"name": name, "entity_type": name})
        svc["metadata"].setdefault("entity_types", []).append({
            "name": name,
            "namespace": "Custom",
            "properties": [{"name": c, "type": "Edm.String", "nullable": True} for c in (allowed_columns or [])],
            "keys": [],
            "navigation_properties": [],
        })
        text = (
            f"Service: {svc['name']}. Entity set: {name} (Custom). "
            f"Description: {description}. Derived from {base_entity_set}. "
            f"Columns: {', '.join(allowed_columns or [])}."
        )
        vector_store.index_tool(
            tool_id=f"{service_id}::{name}",
            text=text,
            metadata={
                "service_id": service_id,
                "service_name": svc["name"],
                "entity_set": name,
                "entity_type": "CustomEntity",
                "properties": allowed_columns or [],
                "is_custom": True,
                "base_entity_set": base_entity_set,
            },
        )
        logger.info(f"Registered custom entity '{name}' on {service_id} (base: {base_entity_set})")
        try:
            from app.mcp.mcp_server import mcp_server
            mcp_server.register_custom_entity_tool(service_id, name, description, allowed_columns or [], base_entity_set)
        except Exception as e:
            logger.warning(f"Failed to register MCP tool for {name}: {e}")
        return custom_def

    def list_custom_entities(self, service_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if service_id:
            return list(self._custom_entities.get(service_id, {}).values())
        out = []
        for sid, entities in self._custom_entities.items():
            out.extend(entities.values())
        return out

    def get_custom_entity(self, service_id: str, name: str) -> Optional[Dict[str, Any]]:
        return self._custom_entities.get(service_id, {}).get(name)

    def delete_custom_entity(self, service_id: str, name: str) -> bool:
        if service_id in self._custom_entities and name in self._custom_entities[service_id]:
            del self._custom_entities[service_id][name]
            meta = self._services.get(service_id, {}).get("metadata", {})
            meta["entity_sets"] = [es for es in meta.get("entity_sets", []) if es.get("name") != name]
            meta["entity_types"] = [et for et in meta.get("entity_types", []) if et.get("name") != name]
            logger.info(f"Deleted custom entity '{name}' from {service_id}")
            try:
                from app.mcp.mcp_server import mcp_server
                mcp_server.remove_custom_entity_tool(service_id, name)
            except Exception as e:
                logger.warning(f"Failed to remove MCP tool for {name}: {e}")
            try:
                g = self.graph()
                g.delete_entity(service_id, name)
            except Exception as e:
                logger.warning(f"Failed to delete custom entity from graph: {e}")
            return True
        return False

    def resolve_custom_entity(self, service_id: str, entity_set: str) -> Optional[Dict[str, Any]]:
        return self._custom_entities.get(service_id, {}).get(entity_set)


service_manager = ODataServiceManager()
