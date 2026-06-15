"""OData v4 / v2-compatible client.

Implements metadata discovery (CSDL or simplified service document),
entity reads with $select / $filter / $expand / $top / $skip / $orderby /
$count, and a fallback "schema sampling" pass for services whose metadata
doesn't include full type definitions (e.g. the public ODataSamples
Northwind service).
"""
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
import httpx
from loguru import logger


NS = {
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
    "schema": "http://docs.oasis-open.org/odata/ns/edm",
    "data": "http://docs.oasis-open.org/odata/ns/data",
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "app": "http://www.w3.org/2007/app",
    "atom": "http://www.w3.org/2005/Atom",
    "m": "http://docs.oasis-open.org/odata/ns/metadata",
}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


class ODataClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._metadata_cache: Optional[Dict[str, Any]] = None
        self._sampled: bool = False
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def get_metadata(self, force_refresh: bool = False) -> Dict[str, Any]:
        if self._metadata_cache and not force_refresh:
            return self._metadata_cache
        url = f"{self.base_url}/$metadata"
        client = await self._get_client()
        resp = await client.get(url, headers={"Accept": "application/xml"})
        resp.raise_for_status()
        xml_text = resp.text
        meta = self._parse_metadata(xml_text)
        await self._enrich_by_sampling(meta)
        self._metadata_cache = meta
        return self._metadata_cache

    def _parse_metadata(self, xml_text: str) -> Dict[str, Any]:
        root = ET.fromstring(xml_text)
        entity_types: Dict[str, Dict[str, Any]] = {}
        entity_sets: List[Dict[str, Any]] = []
        associations: List[Dict[str, Any]] = []
        namespace = ""

        schemas = list(root.iter(f"{{{NS['schema']}}}Schema"))
        if not schemas:
            for child in root.iter():
                if _strip_ns(child.tag) == "Schema":
                    schemas.append(child)

        for schema in schemas:
            ns = schema.attrib.get("Namespace", "")
            if not namespace:
                namespace = ns
            for et in schema.findall(f"{{{NS['schema']}}}EntityType"):
                name = et.attrib.get("Name")
                if not name:
                    continue
                props = []
                for prop in et.findall(f"{{{NS['schema']}}}Property"):
                    props.append({
                        "name": prop.attrib.get("Name"),
                        "type": prop.attrib.get("Type"),
                        "nullable": prop.attrib.get("Nullable", "true") == "true",
                    })
                keys = [n.text for n in et.findall(f"{{{NS['schema']}}}Key/{{{NS['schema']}}}PropertyRef")]
                nav_props = []
                for nav in et.findall(f"{{{NS['schema']}}}NavigationProperty"):
                    nav_props.append({
                        "name": nav.attrib.get("Name"),
                        "type": nav.attrib.get("Type"),
                        "partner": nav.attrib.get("Partner"),
                    })
                entity_types[name] = {
                    "name": name,
                    "namespace": ns,
                    "properties": props,
                    "keys": keys,
                    "navigation_properties": nav_props,
                }

        for container in root.iter(f"{{{NS['schema']}}}EntityContainer"):
            for es in container.findall(f"{{{NS['schema']}}}EntitySet"):
                entity_sets.append({
                    "name": es.attrib.get("Name"),
                    "entity_type": es.attrib.get("EntityType"),
                })

        for assoc in root.iter(f"{{{NS['schema']}}}Association"):
            ends = assoc.findall(f"{{{NS['schema']}}}End")
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
        return f"{self.base_url}/{entity_set}{'?' + qs if qs else ''}"

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
        url = self._build_url(
            entity_set, select=select, filter_expr=filter_expr,
            expand=expand, top=top, skip=skip, orderby=orderby, count=True,
        )
        client = await self._get_client()
        resp = await client.get(url, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        return data

    async def get_by_id(self, entity_set: str, entity_id: str, select: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        qs = f"?$select={','.join(select)}" if select else ""
        url = f"{self.base_url}/{entity_set}({entity_id}){qs}"
        client = await self._get_client()
        resp = await client.get(url, headers={"Accept": "application/json"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def flatten_odata_value(value: Any) -> List[Dict[str, Any]]:
        if isinstance(value, dict) and "value" in value:
            return value["value"]
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
