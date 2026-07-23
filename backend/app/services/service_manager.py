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


KNOWN_RELATIONSHIPS: Dict[str, List[Dict[str, Any]]] = {}


class ODataServiceManager:
    def __init__(self):
        self._services: Dict[str, Dict[str, Any]] = {}
        self._clients: Dict[str, ODataClient] = {}
        self._entity_to_set: Dict[str, Dict[str, str]] = {}
        self._custom_entities: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._healthy_entities: Dict[str, Dict[str, bool]] = {}
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
        auth_type: Optional[str] = None,
        auth_config: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            client = ODataClient(base_url, auth_type=auth_type, auth_config=auth_config)
            try:
                meta = await client.get_metadata()
            except Exception as e:
                logger.warning(f"Failed to fetch metadata for {name} ({base_url}): {e}")
                meta = {"entity_types": [], "entity_sets": [], "associations": [], "namespace": ""}
                # Try to recover entities from Neo4j (may exist from a previous registration)
                try:
                    g = self.graph()
                    existing = g.get_service_entities(service_id)
                    logger.info(f"Neo4j entity recovery for {service_id}: found {len(existing) if existing else 0} entities")
                    if existing:
                        logger.info(f"Recovered {len(existing)} entities for {service_id} from Neo4j")
                        for ent in existing:
                            meta["entity_sets"].append({"name": ent["name"], "entity_type": ent.get("type", ent["name"])})
                            props = ent.get("properties", [])
                            ent_label = ent.get("label", "")
                            prop_labels = ent.get("property_labels", {})
                            if props:
                                et_name = ent.get("type", ent["name"]).split(".")[-1]
                                meta["entity_types"].append({
                                    "name": et_name,
                                    "namespace": "",
                                    "label": ent_label,
                                    "properties": [{"name": p, "type": "Edm.String", "nullable": True, "label": prop_labels.get(p, "")} for p in props],
                                })
                except Exception as ex:
                    logger.warning(f"Failed to recover entities from Neo4j for {service_id}: {ex}")
            self._services[service_id] = {
                "id": service_id,
                "name": name,
                "base_url": base_url,
                "description": description,
                "metadata": meta,
                "metadata_xml": getattr(client, "metadata_xml", ""),
                "extra": metadata or {},
                "auth_type": auth_type,
                "auth_config": auth_config,
            }
            self._clients[service_id] = client
            self._index_service_in_graph(service_id, self._services[service_id])
            self._index_service_in_vector_store(service_id, self._services[service_id])
            asyncio.create_task(self._health_check_entities(service_id))
            return self._services[service_id]

    async def _health_check_entities(self, service_id: str):
        svc = self._services.get(service_id)
        if not svc:
            return
        client = self._clients.get(service_id)
        if not client:
            return
        entity_sets = svc["metadata"].get("entity_sets", [])
        healthy: Dict[str, bool] = {}
        sem = asyncio.Semaphore(5)
        http_client = await client._get_client()
        async def _test_one(es_name: str):
            async with sem:
                try:
                    if client._is_sap_cpi():
                        url = client._build_sap_cpi_url(es_name, top=1)
                    else:
                        url = f"{svc['base_url']}/{es_name}?$top=1"
                    headers = client._get_auth_headers()
                    resp = await http_client.get(url, headers=headers, timeout=15)
                    healthy[es_name] = resp.status_code == 200
                    if resp.status_code != 200:
                        logger.warning(f"Health check {service_id}/{es_name}: HTTP {resp.status_code}")
                except Exception as e:
                    healthy[es_name] = False
                    logger.warning(f"Health check {service_id}/{es_name}: {type(e).__name__}: {e}")
        await asyncio.gather(*[_test_one(es["name"]) for es in entity_sets])
        self._healthy_entities[service_id] = healthy
        working = sum(1 for v in healthy.values() if v)
        svc["healthy_entity_sets"] = [name for name, ok in healthy.items() if ok]
        svc["unhealthy_entity_sets"] = [name for name, ok in healthy.items() if not ok]
        logger.info(f"Health check {service_id}: {working}/{len(healthy)} entities healthy")

    def get_healthy_entities(self, service_id: str) -> List[str]:
        h = self._healthy_entities.get(service_id, {})
        return [name for name, ok in h.items() if ok]

    def _index_service_in_graph(self, service_id: str, svc: Dict[str, Any]):
        g = self.graph()
        g.upsert_service({
            "id": service_id,
            "name": svc["name"],
            "base_url": svc["base_url"],
            "description": svc["description"],
            "metadata": svc.get("extra", {}),
            "auth_type": svc.get("auth_type"),
            "auth_config": svc.get("auth_config"),
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
            entity_label = et.get("label", "") if et else ""
            allowed_ops = ["select", "filter", "expand", "orderby", "top", "skip"]
            g.upsert_entity({
                "service_id": service_id,
                "name": es["name"],
                "type": et_name,
                "label": entity_label,
                "description": f"Entity set {es['name']} of {et_name}. {svc['description']}",
                "allowed_ops": allowed_ops,
                "properties": [p["name"] for p in props],
                "property_labels": {p["name"]: p.get("label", "") for p in props},
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
            entity_labels = {}
            et_list = svc["metadata"].get("entity_types", [])
            et_names = {e["name"] for e in et_list}
            for es in svc["metadata"].get("entity_sets", []):
                es_name = es["name"]
                et_name = es.get("entity_type", es_name)
                et = next((e for e in et_list if e["name"] == et_name), None)
                if not et and "." in et_name:
                    local_name = et_name.rsplit(".", 1)[-1]
                    et = next((e for e in et_list if e["name"] == local_name), None)
                if not et:
                    # Try partial match: entity_type ends with entity set name
                    et = next((e for e in et_list if et_name.endswith(e["name"])), None)
                props = [p["name"] for p in (et or {}).get("properties", [])]
                prop_labels = {p["name"]: p.get("label", "") for p in (et or {}).get("properties", [])}
                entity_props[es_name] = props
                entity_labels[es_name] = {
                    "entity_label": (et or {}).get("label", ""),
                    "property_labels": prop_labels,
                }
            out.append({
                "id": sid,
                "name": svc["name"],
                "base_url": svc["base_url"],
                "description": svc["description"],
                "entity_sets": [es["name"] for es in svc["metadata"].get("entity_sets", [])],
                "entity_properties": entity_props,
                "entity_labels": entity_labels,
                "healthy_entity_sets": svc.get("healthy_entity_sets"),
                "unhealthy_entity_sets": svc.get("unhealthy_entity_sets"),
            })
        return out

    async def recover_from_graph(self):
        """Restore service registrations from the graph DB and refresh
        metadata from the upstream OData endpoint. Used at backend startup
        so the in-memory service map stays consistent across restarts.
        Also restores custom entities and re-registers MCP tools.
        """
        g = self.graph()
        if hasattr(g, '_driver') and g._driver is None:
            logger.info("Neo4j was unavailable at startup, retrying connection...")
            for attempt in range(5):
                g._connect(retries=3, delay=5)
                g = self.graph()
                if hasattr(g, '_driver') and g._driver is not None:
                    logger.info(f"Neo4j connected on attempt {attempt+1}")
                    break
                logger.info(f"Neo4j still unavailable, waiting 10s before retry ({attempt+1}/5)...")
                import time as _time
                _time.sleep(10)
        try:
            persisted = g.list_all_services()
        except Exception as e:
            logger.warning(f"Could not read services from graph: {e}")
            return

        async def _register_one(svc):
            sid = svc.get("id")
            base_url = svc.get("base_url")
            name = svc.get("name")
            description = svc.get("description", "")
            if not sid or not base_url:
                return
            if sid in self._services:
                return
            auth_type = svc.get("auth_type")
            auth_config_str = svc.get("auth_config")
            auth_config = None
            if auth_config_str and auth_config_str != "None":
                import ast
                try:
                    auth_config = ast.literal_eval(auth_config_str)
                except Exception:
                    auth_config = None
            try:
                logger.info(f"Recovering service {sid} from graph ...")
                await self.register_service(
                    service_id=sid,
                    name=name or sid,
                    base_url=base_url,
                    description=description,
                    auth_type=auth_type,
                    auth_config=auth_config,
                )
                logger.info(f"  {sid}: recovered")
            except Exception as e:
                logger.warning(f"  Failed to recover {sid}: {e}")

        tasks = [_register_one(svc) for svc in persisted]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
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
        # Handle both standard OData {"value": [...]} and SAP CPI {"EntityType": [...]}
        if isinstance(raw, dict):
            rows = raw.get("value", [])
            if not rows:
                for v in raw.values():
                    if isinstance(v, list):
                        rows = v
                        break
        else:
            rows = []
        total_count = raw.get("@odata.count") if isinstance(raw, dict) else None
        url = execution["url"]

        base_url = url.split("?")[0] if "?" in url else url

        top_limit = None
        if "?" in url:
            import re as _re
            top_match = _re.search(r'(?:\$top|%24top)=(\d+)', url)
            if top_match:
                top_limit = int(top_match.group(1))

        # SAP CPI doesn't support $skip/$top pagination — skip it
        is_sap_cpi = client._is_sap_cpi() if hasattr(client, '_is_sap_cpi') else False
        effective_max = min(max_rows, top_limit) if top_limit else max_rows
        if not is_sap_cpi and total_count and total_count > len(rows) and total_count <= effective_max:
            page_size = len(rows) if len(rows) > 0 else 20
            skip = len(rows)
            while skip < total_count and skip < effective_max:
                try:
                    page_size_actual = min(page_size, effective_max - skip)
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

        if total_count and len(rows) > effective_max:
            rows = rows[:effective_max]
        elif top_limit and len(rows) > top_limit:
            rows = rows[:top_limit]

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
        # Smart column ordering: important identifiers, names, dates first
        # Force critical columns to always be first
        critical_prefixes = {"purchaseorder", "orderid", "orderno", "customerid", "productid",
                             "supplierid", "employeeid", "orderid", "shipperid", "categoryid"}
        important_keywords = {"order", "number", "id", "name", "code", "date", "status", "type",
                              "amount", "total", "price", "qty", "quantity", "supplier", "customer",
                              "product", "material", "currency", "plant", "company", "organization",
                              "group", "description", "text"}
        skip_keywords = {"isend", "has", "flag", "block", "completeness", "deletion", "correspnc",
                         "manual", "incoterms", "purg", "agr", "conform", "complnc", "quotation",
                         "resp", "person", "phone", "endofpurpose"}
        critical_cols = [c for c in columns if any(c.lower().startswith(cp) for cp in critical_prefixes)]
        priority_cols = [c for c in columns if c not in critical_cols and any(kw in c.lower() for kw in important_keywords) and not any(sk in c.lower() for sk in skip_keywords)]
        other_cols = [c for c in columns if c not in critical_cols and c not in priority_cols]
        columns = critical_cols + priority_cols + other_cols
        if len(columns) > 40:
            columns = columns[:40]
        cleaned_rows = [{k: v for k, v in r.items() if k in columns} for r in cleaned_rows]

        # Build column_labels mapping from entity metadata
        column_labels = {}
        entity_set = plan.get("entity_set", "")
        entity_labels = {}
        for svc_data in self.list_services():
            if svc_data["id"] == service_id:
                entity_labels = svc_data.get("entity_labels", {})
                break
        labels_info = entity_labels.get(entity_set, {})
        if not labels_info and entity_set:
            es_lower = entity_set.lower()
            for key, val in entity_labels.items():
                if key.lower() == es_lower or key.lower().endswith(es_lower) or es_lower.endswith(key.lower()):
                    labels_info = val
                    break
        prop_labels = labels_info.get("property_labels", {})
        for col in columns:
            if col in prop_labels and prop_labels[col]:
                column_labels[col] = prop_labels[col]

        sanitized = {
            "columns": columns,
            "rows": cleaned_rows,
            "row_count": len(cleaned_rows),
            "truncated": (total_count or len(cleaned_rows)) > len(cleaned_rows),
            "total_count": total_count,
            "column_labels": column_labels if column_labels else None,
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

    def delete_service(self, service_id: str) -> bool:
        if service_id not in self._services:
            return False
        del self._services[service_id]
        self._clients.pop(service_id, None)
        self._entity_to_set.pop(service_id, None)
        self._custom_entities.pop(service_id, None)
        try:
            g = self.graph()
            g.delete_service(service_id)
        except Exception as e:
            logger.warning(f"Failed to delete service from graph: {e}")
        logger.info(f"Deleted service '{service_id}'")
        return True

    def resolve_custom_entity(self, service_id: str, entity_set: str) -> Optional[Dict[str, Any]]:
        return self._custom_entities.get(service_id, {}).get(entity_set)


service_manager = ODataServiceManager()
