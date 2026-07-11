"""Per-entity column priority map derived from SAP OData $metadata annotations.

Parses @UI.importance, @UI.lineItem, @UI.selectionField, sap:label, Key="true",
sap:filterable, sap:sortable annotations to rank columns by business relevance.

Falls back to a heuristic ranking when annotations are sparse/missing.
"""
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger


# ── Cache ────────────────────────────────────────────────────────────────────

class _PriorityCache:
    """In-memory TTL cache for column priority maps."""

    def __init__(self, ttl: float = 86400.0, max_size: int = 128):
        self._cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._ttl = ttl
        self._max_size = max_size

    def get(self, key: str) -> Optional[List[Dict[str, Any]]]:
        if key in self._cache:
            ts, val = self._cache[key]
            if time.monotonic() - ts < self._ttl:
                return val
            del self._cache[key]
        return None

    def set(self, key: str, value: List[Dict[str, Any]]):
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[key] = (time.monotonic(), value)

    def invalidate(self, key: str):
        self._cache.pop(key, None)

    def clear(self):
        self._cache.clear()


_priority_cache = _PriorityCache()


# ── SAP metadata annotation parser ───────────────────────────────────────────

_NS = {
    "edm": "http://docs.oasis-open.org/odata/ns/edm",
    "edmx": "http://docs.oasis-open.org/odata/ns/edmx",
    "sap": "http://www.sap.com/Protocols/SAPData",
    "common": "http://docs.oasis-open.org/odata/ns/edm",
    "ui": "http://docs.oasis-open.org/odata/ns/edm",
}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _get_attr(el: ET.Element, attr_name: str, ns_map: Dict[str, str] = None) -> str:
    """Get attribute value trying full namespace, short prefix, and bare name."""
    if ns_map:
        for prefix, uri in ns_map.items():
            val = el.attrib.get(f"{{{uri}}}{attr_name}")
            if val:
                return val
    val = el.attrib.get(attr_name)
    if val:
        return val
    for k, v in el.attrib.items():
        if _strip_ns(k) == attr_name:
            return v
    return ""


def _parse_annotations_from_metadata(xml_text: str, entity_set_name: str) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Parse SAP OData $metadata XML and extract per-field annotations.

    Returns dict: { property_name: { annotation_key: annotation_value } }
    """
    root = ET.fromstring(xml_text)
    field_annotations: Dict[str, Dict[str, str]] = {}

    # Build namespace map from root
    ns_map = dict(_NS)
    for event, elem in ET.iterparse(
        __import__("io").BytesIO(xml_text.encode("utf-8")),
        events=["start-ns"],
    ):
        prefix, uri = elem
        if prefix and uri:
            ns_map[prefix] = uri

    # Find the entity type for the given entity set
    edm_ns = ns_map.get("edm", "http://docs.oasis-open.org/odata/ns/edm")
    target_entity_type = None

    for schema in root.iter(f"{{{edm_ns}}}Schema"):
        for es in schema.iter(f"{{{edm_ns}}}EntitySet"):
            if es.attrib.get("Name") == entity_set_name:
                target_entity_type = es.attrib.get("EntityType", "")
                break
        if target_entity_type:
            break

    # Fallback: namespace-agnostic search
    if not target_entity_type:
        for es in root.iter():
            if _strip_ns(es.tag) == "EntitySet" and es.attrib.get("Name") == entity_set_name:
                target_entity_type = es.attrib.get("EntityType", "")
                break

    if not target_entity_type:
        return field_annotations

    # Strip namespace prefix from entity type name for matching
    short_type = target_entity_type.rsplit(".", 1)[-1] if "." in target_entity_type else target_entity_type

    # Find the EntityType definition
    for schema in root.iter(f"{{{edm_ns}}}Schema"):
        for et in schema.iter(f"{{{edm_ns}}}EntityType"):
            et_name = et.attrib.get("Name", "")
            if et_name == short_type or et_name == target_entity_type:
                keys = set()
                for key_el in et.findall(f"{{{edm_ns}}}Key/{{{edm_ns}}}PropertyRef"):
                    keys.add(key_el.attrib.get("Name", ""))

                for prop in et.findall(f"{{{edm_ns}}}Property"):
                    prop_name = prop.attrib.get("Name", "")
                    annos: Dict[str, str] = {}

                    # Key field
                    if prop_name in keys:
                        annos["isKey"] = "true"

                    # sap:label
                    label = _get_attr(prop, "label", ns_map)
                    if label:
                        annos["label"] = label

                    # sap:filterable
                    filterable = _get_attr(prop, "filterable", ns_map)
                    if filterable:
                        annos["isFilterable"] = filterable.lower() == "true"

                    # sap:sortable
                    sortable = _get_attr(prop, "sortable", ns_map)
                    if sortable:
                        annos["isSortable"] = sortable.lower() == "true"

                    # sap:required
                    required = _get_attr(prop, "required", ns_map)
                    if required:
                        annos["isRequired"] = required.lower() == "true"

                    # sap:unit for currency/quantity fields
                    unit = _get_attr(prop, "unit", ns_map)
                    if unit:
                        annos["unitField"] = unit

                    field_annotations[prop_name] = annos

                # Now look for Annotations elements (OData V4 style)
                for ann_group in schema.iter(f"{{{edm_ns}}}Annotations"):
                    target = ann_group.attrib.get("Target", "")
                    if not target.endswith(short_type) and not target.endswith(target_entity_type):
                        continue
                    for ann in ann_group.iter(f"{{{edm_ns}}}Annotation"):
                        term = ann.attrib.get("Term", "")
                        # @UI.LineItem
                        if "LineItem" in term:
                            for rec in ann.iter(f"{{{edm_ns}}}Record"):
                                for prop_val in rec.iter(f"{{{edm_ns}}}PropertyValue"):
                                    field_name = prop_val.attrib.get("Property", "")
                                    if field_name and field_name in field_annotations:
                                        field_annotations[field_name]["isLineItem"] = "true"
                        # @UI.SelectionField
                        elif "SelectionField" in term:
                            for rec in ann.iter(f"{{{edm_ns}}}Record"):
                                for prop_val in rec.iter(f"{{{edm_ns}}}PropertyValue"):
                                    field_name = prop_val.attrib.get("Property", "")
                                    if field_name and field_name in field_annotations:
                                        field_annotations[field_name]["isSelectionField"] = "true"
                        # @UI.Importance
                        elif "Importance" in term or "importance" in term.lower():
                            for rec in ann.iter(f"{{{edm_ns}}}Record"):
                                target_field = ""
                                importance = ""
                                for prop_val in rec.iter(f"{{{edm_ns}}}PropertyValue"):
                                    pname = prop_val.attrib.get("Property", "")
                                    pval = prop_val.attrib.get("String", "") or prop_val.attrib.get("EnumMember", "")
                                    if "Target" in pname or "Field" in pname:
                                        target_field = pval.rsplit(".", 1)[-1] if "." in pval else pval
                                    if "Importance" in pname or "importance" in pname.lower():
                                        importance = pval.rsplit(".", 1)[-1] if "." in pval else pval
                                if target_field and target_field in field_annotations:
                                    field_annotations[target_field]["importance"] = importance
                        # @Common.Label
                        elif "Label" in term:
                            for rec in ann.iter(f"{{{edm_ns}}}Record"):
                                for prop_val in rec.iter(f"{{{edm_ns}}}PropertyValue"):
                                    field_name = prop_val.attrib.get("Property", "")
                                    label_val = prop_val.attrib.get("String", "")
                                    if field_name and field_name in field_annotations and label_val:
                                        field_annotations[field_name]["label"] = label_val

                break
        if field_annotations:
            break

    return field_annotations


# ── Fallback heuristic ───────────────────────────────────────────────────────

_DEPRIORITIZE_PATTERNS = [
    r"CreationTime$",
    r"CreatedAt$",
    r"ChangedAt$",
    r"ChangedBy$",
    r"LastChangedAt$",
    r"LastChangedBy$",
    r"Timestamp$",
    r"ETag$",
    r"Internal$",
    r"Guid$",
    r"UUID$",
]

_BOOST_PATTERNS = [
    (r"ID$", 1),
    (r"Name$", 2),
    (r"Date$", 2),
    (r"Text$", 2),
    (r"Quantity$", 2),
    (r"Amount$", 2),
    (r"Price$", 2),
    (r"Status$", 2),
    (r"Order", 1),
    (r"Material", 1),
    (r"Plant", 1),
    (r"Customer", 1),
    (r"Supplier", 1),
    (r"Product", 1),
]

_ID_PATTERN = re.compile(r"ID$|Id$")


def _fallback_rank(field_name: str, annos: Dict[str, str]) -> int:
    """Assign a numeric rank (lower = higher priority) based on heuristics.

    Rank 1: key fields, ID fields, core business fields
    Rank 2: filterable fields, named/date/quantity fields
    Rank 3: everything else
    Deprioritized fields get rank 99
    """
    name_lower = field_name.lower()

    # Deprioritize timestamp/audit fields
    for pat in _DEPRIORITIZE_PATTERNS:
        if re.search(pat, field_name, re.IGNORECASE):
            return 99

    # Rank 1: key fields and ID fields
    if annos.get("isKey") == "true":
        return 1
    if _ID_PATTERN.search(field_name):
        return 1

    # Boost core business fields
    for pat, rank in _BOOST_PATTERNS:
        if re.search(pat, field_name, re.IGNORECASE):
            return rank

    # Rank 2: filterable fields
    if annos.get("isFilterable") == "true":
        return 2

    # Rank 3: everything else
    return 3


def _build_priority_list(
    field_annotations: Dict[str, Dict[str, str]],
    all_fields: List[str],
) -> List[Dict[str, Any]]:
    """Build a sorted priority list from annotations + fallback heuristic."""
    result = []

    for field in all_fields:
        annos = field_annotations.get(field, {})
        entry: Dict[str, Any] = {
            "field": field,
            "label": annos.get("label", field),
            "importance": annos.get("importance", ""),
            "isKey": annos.get("isKey") == "true",
            "isLineItem": annos.get("isLineItem") == "true",
            "isSelectionField": annos.get("isSelectionField") == "true",
            "isFilterable": annos.get("isFilterable") == "true",
            "isSortable": annos.get("isSortable") == "true",
        }

        # Determine sort key
        has_ui_annotations = any([
            annos.get("importance"),
            annos.get("isLineItem"),
            annos.get("isSelectionField"),
        ])

        if has_ui_annotations:
            # UI annotations present — sort by importance
            imp = annos.get("importance", "").upper()
            imp_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(imp, 1)
            sort_key = (0, imp_rank, field)
        else:
            # No UI annotations — use fallback heuristic
            fallback = _fallback_rank(field, annos)
            sort_key = (1, fallback, field)

        entry["_sort_key"] = sort_key
        result.append(entry)

    result.sort(key=lambda x: x["_sort_key"])
    for item in result:
        del item["_sort_key"]

    return result


# ── Public API ───────────────────────────────────────────────────────────────

def get_column_priorities(
    entity_set_name: str,
    service_id: str = "",
    all_fields: Optional[List[str]] = None,
    metadata_xml: Optional[str] = None,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """Get prioritized column list for an entity set.

    Args:
        entity_set_name: OData entity set name (e.g. "C_ManageProductionOrder")
        service_id: Service identifier for cache keying
        all_fields: Full list of field names (from entity_sets metadata)
        metadata_xml: Raw $metadata XML text (fetched if not provided)
        force_refresh: Bypass cache

    Returns:
        Sorted list of column priority entries.
    """
    cache_key = f"{service_id}/{entity_set_name}" if service_id else entity_set_name

    if not force_refresh:
        cached = _priority_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"Column priority cache hit for {cache_key}")
            return cached

    field_annotations: Dict[str, Dict[str, str]] = {}

    if metadata_xml:
        try:
            field_annotations = _parse_annotations_from_metadata(metadata_xml, entity_set_name)
        except Exception as e:
            logger.warning(f"Failed to parse metadata annotations for {entity_set_name}: {e}")

    if not all_fields and field_annotations:
        all_fields = list(field_annotations.keys())

    if not all_fields:
        return []

    result = _build_priority_list(field_annotations, all_fields)
    _priority_cache.set(cache_key, result)

    ui_count = sum(1 for r in result if r.get("isLineItem") or r.get("isSelectionField") or r.get("importance"))
    logger.info(
        f"Column priorities for {entity_set_name}: {len(result)} fields, "
        f"{ui_count} with UI annotations, "
        f"{sum(1 for r in result if r.get('isKey'))} keys"
    )

    return result


def invalidate_column_priority_cache(entity_set_name: str, service_id: str = ""):
    """Force-refresh the priority cache for a specific entity set."""
    cache_key = f"{service_id}/{entity_set_name}" if service_id else entity_set_name
    _priority_cache.invalidate(cache_key)
    logger.info(f"Invalidated column priority cache for {cache_key}")


def get_top_columns(
    entity_set_name: str,
    service_id: str = "",
    all_fields: Optional[List[str]] = None,
    metadata_xml: Optional[str] = None,
    max_columns: int = 20,
    force_refresh: bool = False,
) -> List[str]:
    """Return just the top-N field names for default query building."""
    priorities = get_column_priorities(
        entity_set_name, service_id, all_fields, metadata_xml, force_refresh
    )
    return [p["field"] for p in priorities[:max_columns]]


def get_default_columns(
    entity_set_name: str,
    service_id: str = "",
    all_fields: Optional[List[str]] = None,
    metadata_xml: Optional[str] = None,
) -> List[str]:
    """Return default columns: Rank 1 (keys + ID) + Rank 2 (filterable) fields."""
    priorities = get_column_priorities(
        entity_set_name, service_id, all_fields, metadata_xml
    )
    return [p["field"] for p in priorities if not p["field"].endswith(("Time", "At", "By", "Guid", "UUID", "ETag"))]


def log_field_selection(
    entity_set: str,
    service_id: str,
    query: str,
    selected_fields: List[str],
    total_fields: int,
):
    """Log which fields were selected for a query (for usage-based tuning)."""
    logger.info(
        f"FIELD_SELECTION | service={service_id} | entity={entity_set} | "
        f"selected={len(selected_fields)}/{total_fields} | "
        f"fields={selected_fields[:10]}{'...' if len(selected_fields) > 10 else ''} | "
        f"query={query[:80]}"
    )
