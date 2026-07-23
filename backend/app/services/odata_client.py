"""OData v4 / v2-compatible client.

Implements metadata discovery (CSDL or simplified service document),
entity reads with $select / $filter / $expand / $top / $skip / $orderby /
$count, and a fallback "schema sampling" pass for services whose metadata
doesn't include full type definitions (e.g. the public ODataSamples
Northwind service).
"""
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
import httpx
from loguru import logger


class _QueryCache:
    """Simple in-memory TTL cache for OData query results."""

    def __init__(self, ttl: float = 60.0, max_size: int = 256):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._ttl = ttl
        self._max_size = max_size

    def _make_key(self, entity_set: str, select, filter_expr, expand, top, skip, orderby) -> str:
        return f"{entity_set}|{tuple(select or [])}|{filter_expr}|{tuple(expand or [])}|{top}|{skip}|{orderby}"

    def get(self, entity_set: str, select, filter_expr, expand, top, skip, orderby) -> Optional[Any]:
        key = self._make_key(entity_set, select, filter_expr, expand, top, skip, orderby)
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        if entry:
            del self._cache[key]
        return None

    def set(self, entity_set: str, select, filter_expr, expand, top, skip, orderby, value):
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest_key]
        key = self._make_key(entity_set, select, filter_expr, expand, top, skip, orderby)
        self._cache[key] = (time.monotonic(), value)


NS = {
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
    "schema": "http://docs.oasis-open.org/odata/ns/edm",
    "data": "http://docs.oasis-open.org/odata/ns/data",
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "app": "http://www.w3.org/2007/app",
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://docs.oasis-open.org/odata/ns/metadata",
    "sap": "http://www.sap.com/Protocols/SAPData",
}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


class ODataClient:
    _query_cache = _QueryCache(ttl=60.0, max_size=256)

    def __init__(self, base_url: str, timeout: float = 30.0, auth_type: str = None, auth_config: Dict[str, str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._metadata_cache: Optional[Dict[str, Any]] = None
        self._metadata_xml: str = ""
        self._sampled: bool = False
        self._client: Optional[httpx.AsyncClient] = None
        self._auth_type = auth_type
        self._auth_config = auth_config or {}

    def _get_auth_headers(self) -> Dict[str, str]:
        headers = {}
        if self._auth_type == "basic" and self._auth_config:
            import base64
            user = self._auth_config.get("username", "")
            pwd = self._auth_config.get("password", "")
            if user:
                token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
                logger.info(f"OData auth: Basic Auth for user '{user}'")
        elif self._auth_type == "bearer" and self._auth_config:
            token = self._auth_config.get("token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
                logger.info(f"OData auth: Bearer token")
        elif self._auth_type == "api_key" and self._auth_config:
            key_name = self._auth_config.get("header_name", "X-API-Key")
            key_value = self._auth_config.get("api_key", "")
            if key_value:
                headers[key_name] = key_value
                logger.info(f"OData auth: API Key in '{key_name}'")
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def get_metadata(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self._metadata_cache and not force_refresh:
            return self._metadata_cache
        # SAP CPI pattern: metadata=true in query returns XML at service root
        if "metadata=true" in self.base_url.lower():
            url = self.base_url
        elif "?" in self.base_url:
            base, qs = self.base_url.split("?", 1)
            url = f"{base}/$metadata?{qs}"
        else:
            url = f"{self.base_url}/$metadata"
        client = await self._get_client()
        headers = {"Accept": "application/xml"}
        headers.update(self._get_auth_headers())
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        xml_text = resp.text
        self._metadata_xml = xml_text
        meta = self._parse_metadata(xml_text)
        await self._enrich_by_sampling(meta)
        self._metadata_cache = meta
        return self._metadata_cache

    @property
    def metadata_xml(self) -> str:
        """Return raw $metadata XML text (for column priority parsing)."""
        return self._metadata_xml

    def _parse_metadata(self, xml_text: str) -> Dict[str, Any]:
        root = ET.fromstring(xml_text)
        entity_types: Dict[str, Dict[str, Any]] = {}
        entity_sets: List[Dict[str, Any]] = []
        associations: List[Dict[str, Any]] = []
        namespace = ""

        schemas = list(root.iter(f"{{{NS['schema']}}}Schema"))
        edm_ns = NS['schema']
        if not schemas:
            for child in root.iter():
                if _strip_ns(child.tag) == "Schema":
                    schemas.append(child)
                    # Detect which namespace is used
                    ns_uri = child.tag.split("}")[0].lstrip("{") if "}" in child.tag else ""
                    if ns_uri:
                        edm_ns = ns_uri
                    break

        for schema in schemas:
            ns = schema.attrib.get("Namespace", "")
            if not namespace:
                namespace = ns
            for et in schema.findall(f"{{{edm_ns}}}EntityType"):
                name = et.attrib.get("Name")
                if not name:
                    continue
                # Extract sap:label for entity type
                entity_label = et.attrib.get(f"{{{NS['sap']}}}label", "")
                props = []
                for prop in et.findall(f"{{{edm_ns}}}Property"):
                    # Extract sap:label for property
                    prop_label = prop.attrib.get(f"{{{NS['sap']}}}label", "")
                    props.append({
                        "name": prop.attrib.get("Name"),
                        "type": prop.attrib.get("Type"),
                        "nullable": prop.attrib.get("Nullable", "true") == "true",
                        "label": prop_label,
                    })
                keys = [n.text for n in et.findall(f"{{{edm_ns}}}Key/{{{edm_ns}}}PropertyRef")]
                nav_props = []
                for nav in et.findall(f"{{{edm_ns}}}NavigationProperty"):
                    nav_props.append({
                        "name": nav.attrib.get("Name"),
                        "type": nav.attrib.get("Type"),
                        "partner": nav.attrib.get("Partner"),
                    })
                entity_types[name] = {
                    "name": name,
                    "namespace": ns,
                    "label": entity_label,
                    "properties": props,
                    "keys": keys,
                    "navigation_properties": nav_props,
                }

        for container in root.iter(f"{{{edm_ns}}}EntityContainer"):
            for es in container.findall(f"{{{edm_ns}}}EntitySet"):
                entity_sets.append({
                    "name": es.attrib.get("Name"),
                    "entity_type": es.attrib.get("EntityType"),
                })

        # Fallback: namespace-agnostic EntityContainer detection
        if not entity_sets:
            for container in root.iter():
                if _strip_ns(container.tag) == "EntityContainer":
                    for es in container:
                        if _strip_ns(es.tag) == "EntitySet":
                            entity_sets.append({
                                "name": es.attrib.get("Name"),
                                "entity_type": es.attrib.get("EntityType"),
                            })

        for assoc in root.iter(f"{{{edm_ns}}}Association"):
            ends = assoc.findall(f"{{{edm_ns}}}End")
            if len(ends) >= 2:
                associations.append({
                    "name": assoc.attrib.get("Name"),
                    "end1": {
                        "type": ends[0].attrib.get("Type"),
                        "role": ends[0].attrib.get("Role"),
                        "multiplicity": ends[0].attrib.get("Multiplicity"),
                    },
                    "end2": {
                        "type": ends[1].attrib.get("Type"),
                        "role": ends[1].attrib.get("Role"),
                        "multiplicity": ends[1].attrib.get("Multiplicity"),
                    },
                })

        if not entity_sets and not entity_types:
            for col in root.iter():
                if _strip_ns(col.tag) == "collection":
                    href = col.attrib.get("href")
                    if href:
                        entity_sets.append({"name": href, "entity_type": href})

        if not namespace:
            namespace = self.base_url.rstrip("/").split("/")[-1] or "Default"

        return {
            "namespace": namespace,
            "entity_types": list(entity_types.values()),
            "entity_sets": entity_sets,
            "associations": associations,
        }

    async def _enrich_by_sampling(self, meta: Dict[str, Any]):
        """For services whose metadata doesn't include EntityType definitions
        (e.g. the public ODataSamples Northwind v4 service), fetch one row
        from each entity set to learn its columns.
        """
        et_by_name = {et["name"]: et for et in meta.get("entity_types", [])}
        for es in meta.get("entity_sets", []):
            et_name = (es.get("entity_type") or es.get("name", "")).split(".")[-1]
            if et_name in et_by_name and et_by_name[et_name].get("properties"):
                continue
            try:
                client = await self._get_client()
                resp = await client.get(
                    f"{self.base_url}/{es['name']}?$top=1",
                    headers={"Accept": "application/json"},
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                sample = (data.get("value") or [None])[0]
                if not isinstance(sample, dict):
                    continue
                props = []
                for k, v in sample.items():
                    if k.startswith("@"):
                        continue
                    props.append({
                        "name": k,
                        "type": _infer_type(v),
                        "nullable": v is None,
                    })
                if et_name in et_by_name:
                    et_by_name[et_name]["properties"] = props
                else:
                    et_by_name[et_name] = {
                        "name": et_name,
                        "namespace": meta.get("namespace", "Default"),
                        "properties": props,
                        "keys": [],
                        "navigation_properties": [],
                    }
                    meta["entity_types"].append(et_by_name[et_name])
                if not es.get("entity_type"):
                    es["entity_type"] = et_name
            except Exception as e:
                logger.debug(f"Schema sampling failed for {es['name']}: {e}")

    async def list_entity_sets(self) -> List[Dict[str, Any]]:
        meta = await self.get_metadata()
        return meta.get("entity_sets", [])

    async def get_entity_type(self, entity_type_name: str) -> Optional[Dict[str, Any]]:
        meta = await self.get_metadata()
        for et in meta.get("entity_types", []):
            if et["name"] == entity_type_name or f"{et['namespace']}.{et['name']}" == entity_type_name:
                return et
        return None

    def _get_data_base_url(self) -> str:
        """Get base URL for data queries (strip query params for SAP CPI)."""
        import re
        url = self.base_url
        if self._is_sap_cpi():
            # Strip all query params — they'll be rebuilt by _build_sap_cpi_url
            if "?" in url:
                url = url.split("?")[0]
        elif "metadata=true" in url.lower():
            url = re.sub(r'[&?]metadata=true', '', url, flags=re.IGNORECASE)
            url = re.sub(r'\?$', '', url)
        return url

    def _is_sap_cpi(self) -> bool:
        """Detect SAP CPI pattern (service= param or metadata=true)."""
        url = self.base_url.lower()
        return "service=" in url or "metadata=true" in url

    def _get_sap_service_name(self) -> str:
        """Extract SAP CPI service name from URL (e.g. API_PURCHASEORDER_PROCESS_SRV)."""
        import re
        m = re.search(r'service=([^&]+)', self.base_url, re.IGNORECASE)
        return m.group(1) if m else ""

    def _build_url(
        self,
        entity_set: str,
        select: Optional[List[str]] = None,
        filter_expr: Optional[str] = None,
        expand: Optional[List[str]] = None,
        top: Optional[int] = None,
        skip: Optional[int] = None,
        orderby: Optional[str] = None,
        count: bool = False,
    ) -> str:
        if self._is_sap_cpi():
            return self._build_sap_cpi_url(entity_set, top=top)
        params: List[Tuple[str, str]] = []
        if select:
            params.append(("$select", ",".join(select)))
        if filter_expr:
            params.append(("$filter", filter_expr))
        if expand:
            params.append(("$expand", ",".join(expand)))
        if top is not None:
            params.append(("$top", str(top)))
        if skip is not None:
            params.append(("$skip", str(skip)))
        if orderby:
            params.append(("$orderby", orderby))
        if count:
            params.append(("$count", "true"))
        qs = urlencode(params)
        return f"{self._get_data_base_url()}/{entity_set}{'?' + qs if qs else ''}"

    def _build_sap_cpi_url(self, entity_set: str, top: Optional[int] = None) -> str:
        """Build SAP CPI data URL: base?service=X&entity=Y&top=N"""
        base = self._get_data_base_url()
        service_name = self._get_sap_service_name()
        params = [("service", service_name), ("entity", entity_set)]
        if top is not None:
            params.append(("top", str(top)))
        qs = urlencode(params)
        return f"{base}?{qs}"

    async def query(
        self,
        entity_set: str,
        select: Optional[List[str]] = None,
        filter_expr: Optional[str] = None,
        expand: Optional[List[str]] = None,
        top: Optional[int] = None,
        skip: Optional[int] = None,
        orderby: Optional[str] = None,
    ) -> Dict[str, Any]:
        cached = self._query_cache.get(entity_set, select, filter_expr, expand, top, skip, orderby)
        if cached is not None:
            return cached
        url = self._build_url(
            entity_set, select=select, filter_expr=filter_expr,
            expand=expand, top=top, skip=skip, orderby=orderby, count=True,
        )
        client = await self._get_client()
        headers = {"Accept": "application/json"}
        headers.update(self._get_auth_headers())
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self._query_cache.set(entity_set, select, filter_expr, expand, top, skip, orderby, data)
        return data

    async def get_by_id(self, entity_set: str, entity_id: str, select: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        qs = f"?$select={','.join(select)}" if select else ""
        url = f"{self._get_data_base_url()}/{entity_set}({entity_id}){qs}"
        client = await self._get_client()
        headers = {"Accept": "application/json"}
        headers.update(self._get_auth_headers())
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ─── Write Operations ────────────────────────────────────────────────
    async def create(self, entity_set: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """POST a new entity to the OData service."""
        url = self._build_sap_cpi_url(entity_set) if self._is_sap_cpi() else f"{self._get_data_base_url()}/{entity_set}"
        client = await self._get_client()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(self._get_auth_headers())
        resp = await client.post(url, json=data, headers=headers)
        resp.raise_for_status()
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return {"status": resp.status_code, "success": True, "body": resp.text}
        return {"status": resp.status_code, "success": True}

    async def update(
        self, entity_set: str, entity_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH an existing entity."""
        if self._is_sap_cpi():
            base_url = self._build_sap_cpi_url(entity_set)
            separator = "&" if "?" in base_url else "?"
            url = f"{base_url}{separator}key={entity_id}"
        else:
            url = f"{self._get_data_base_url()}/{entity_set}({entity_id})"
        client = await self._get_client()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(self._get_auth_headers())
        resp = await client.patch(url, json=data, headers=headers)
        resp.raise_for_status()
        if resp.status_code == 204:
            return {"success": True, "operation": "update"}
        return resp.json()

    async def delete(self, entity_set: str, entity_id: str) -> Dict[str, Any]:
        """DELETE an entity."""
        if self._is_sap_cpi():
            base_url = self._build_sap_cpi_url(entity_set)
            separator = "&" if "?" in base_url else "?"
            url = f"{base_url}{separator}key={entity_id}"
        else:
            url = f"{self._get_data_base_url()}/{entity_set}({entity_id})"
        client = await self._get_client()
        headers = {"Accept": "application/json"}
        headers.update(self._get_auth_headers())
        resp = await client.delete(url, headers=headers)
        resp.raise_for_status()
        return {"success": True, "operation": "delete"}

    @staticmethod
    def flatten_odata_value(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        if isinstance(value, dict):
            # SAP CPI wraps data as {"EntityType": [...]}
            for v in value.values():
                if isinstance(v, list):
                    return v
        if isinstance(value, list):
            return value
        return []


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "Edm.Boolean"
    if isinstance(value, int):
        return "Edm.Int32"
    if isinstance(value, float):
        return "Edm.Decimal"
    if isinstance(value, str):
        return "Edm.String"
    if isinstance(value, dict):
        return "Edm.ComplexType"
    if isinstance(value, list):
        return "Collection(Edm.String)"
    return "Edm.String"
