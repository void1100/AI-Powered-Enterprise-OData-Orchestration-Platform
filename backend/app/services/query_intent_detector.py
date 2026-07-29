"""
Query Intent Detector — Identifies precise user intent for:
  1. count_total   — user wants only a total count number
  2. column_select — user wants only specific named columns
  3. None          — normal / general query (no special handling)

Used by the orchestrator and chat router to return exactly what
was asked for — nothing more, nothing less.
"""
import re
from typing import Any, Dict, List, Optional

from loguru import logger


# ── Pattern groups for "give me just the count" ──────────────────────────────

_COUNT_TOTAL_PATTERNS = [
    # "how many products are there" / "how many orders exist"
    r"\bhow many\b.{0,60}",
    # "count of products" / "count products"
    r"\bcount\s+(?:of\s+)?\w+\b",
    # "total number of X"
    r"\btotal\s+number\s+of\b.{0,60}",
    # "number of X"
    r"\bnumber\s+of\b.{0,60}",
    # "give me the count of X"
    r"\bgive\s+me\s+the\s+count\b",
    # "what is the count" / "what's the count"
    r"\bwhat(?:'s| is)\s+the\s+count\b",
    # "count X" at start of query
    r"^count\s+\w+",
    # "total X" where X is a plural entity noun  e.g. "total products"
    r"^total\s+\w+s\b",
]

# Negative signals — if any of these appear, it's NOT a simple total count
# (it's a grouped count, or something else)
_COUNT_TOTAL_NEGATIVES = [
    r"\bper\b",        # "count per customer" → grouped count
    r"\beach\b",       # "count each …"
    r"\bby\b",         # "count by country"
    r"\bgroup\b",      # "group by"
    r"\bwhere\b",      # "count where Status = …" (filtered, but still a count-only, so we keep)
]

# ── Patterns that signal specific-column selection ────────────────────────────

# "show me only ProductName" / "give me just ProductName and UnitPrice"
_COL_SELECT_PATTERNS = [
    # Explicit "only/just" patterns
    r"\b(?:show|give|display|get|fetch|list)\s+(?:me\s+)?(?:only|just|solely)\s+(?:the\s+)?(.+)",
    r"\b(?:only|just)\s+(?:the\s+)?(\w[\w\s,]+?)\s+(?:column|field|attribute)s?\b",
    r"\b(?:show|get|display)\s+(?:me\s+)?(?:the\s+)?(\w[\w\s,]+?)\s+(?:column|field|attribute)s?(?:\s+(?:of|from)\b|$)",
    # "show me X, Y and Z" — list of named columns (must have comma or "and")
    r"\b(?:show|give|display|get|fetch|list)\s+(?:me\s+)?(?:the\s+)?(\w[\w\s,]+?(?:,\s*|\s+and\s+)\w[\w\s,]*?\w)(?=\s+(?:where|from|for|with|order|top|limit|in|and\s+also)|\s*$)",
    # "show me X where..." — single column with trailing clause (avoid entity names)
    r"\b(?:show|give|display|get|fetch|list)\s+(?:me\s+)?(?:the\s+)?([\w\s]+?)\s+(?:where|from|for|with|order|top|limit|in)\b",
    # "what is/are the X" — asking for specific fields
    r"\bwhat\s+(?:is|are)\s+(?:the\s+)?(\w[\w\s,]+?\w)\s*(?:\?|$)",
    # "show manufacturing details where X" — "details" + where clause means all columns filtered
    # (handled separately — not column_select)
    # "display X, Y, Z"
    r"\bdisplay\s+(?:the\s+)?(\w[\w\s,]+?(?:,\s*|\s+and\s+)\w[\w\s,]*?\w)(?=\s*(?:where|from|for|order|$))",
    # "I want to see X and Y"
    r"\b(?:want|need)\s+to\s+see\s+(?:the\s+)?(\w[\w\s,]+?(?:,\s*|\s+and\s+)\w[\w\s,]*?\w)(?=\s*(?:where|from|for|order|$))",
    # "give me X per Y" — specific fields (must have comma or "and")
    r"\bgive\s+me\s+(?:the\s+)?(\w[\w\s,]+?(?:,\s*|\s+and\s+)\w[\w\s,]*?\w)(?=\s+(?:per|by|for|from|where)\b)",
    # "show me X, Y and Z" — comma-separated list (no trailing clause)
    r"\b(?:show|give|display|get|fetch|list)\s+(?:me\s+)?(?:the\s+)?(\w[\w\s,]+?,\s*\w[\w\s,]+?\w)\s*$",
    # "show me X and Y" — two-item list with "and" (no trailing clause)
    r"\b(?:show|give|display|get|fetch|list)\s+(?:me\s+)?(?:the\s+)?(\w[\w\s]+?\w\s+and\s+\w[\w\s]+?\w)\s*$",
]

# Stop-words that should NOT be treated as a column name
_COL_STOP_WORDS = {
    "show", "me", "get", "list", "all", "the", "a", "an", "of", "for",
    "from", "with", "by", "top", "first", "please", "and", "or", "only",
    "just", "solely", "data", "information", "details", "record", "records",
    "rows", "entry", "entries", "result", "results", "where", "order", "sort",
    "limit", "that", "have", "has", "with", "number", "id", "no",
}

# Common business term → OData field name synonyms
# Maps user-friendly names to actual SAP/OData field names
_COL_SYNONYMS = {
    "order number": ["PurchaseOrder", "PurchasingDocument", "OrderID"],
    "po number": ["PurchaseOrder", "PurchasingDocument"],
    "purchase order number": ["PurchaseOrder"],
    "item number": ["PurchaseOrderItem", "PurchasingDocumentItem"],
    "po item": ["PurchaseOrderItem"],
    "serial number": ["SerialNumber", "SerialNumberID"],
    "material number": ["Material", "MaterialNumber"],
    "material": ["Material"],
    "supplier": ["Supplier", "Vendor", "SupplierName"],
    "vendor": ["Supplier", "Vendor", "SupplierName"],
    "plant": ["Plant", "PlantCode"],
    "quantity": ["Quantity", "OrderQuantity", "ScheduleLineOrderQuantity"],
    "price": ["NetPriceAmount", "Price", "ConditionRateValue"],
    "net price": ["NetPriceAmount"],
    "date": ["CreationDate", "PurchaseOrderDate", "RequirementDate"],
    "created on": ["CreationDate", "CreatedByUser"],
    "created by": ["CreatedByUser"],
    "currency": ["DocumentCurrency", "Currency"],
    "unit": ["BaseUnit", "PurchaseOrderQuantityUnit", "Unit"],
    "description": ["PurchaseOrderItemText", "Description", "MaterialName"],
    "text": ["PurchaseOrderItemText", "PlainLongText"],
    "status": ["PurchasingProcessingStatus", "Status"],
    "company code": ["CompanyCode"],
    "purchasing org": ["PurchasingOrganization"],
    "purchasing group": ["PurchasingGroup"],
    "cost center": ["CostCenter"],
    "profit center": ["ProfitCenter"],
    "storage location": ["StorageLocation"],
    "material group": ["MaterialGroup"],
    "gross weight": ["MaterialGrossWeight", "GrossWeight"],
    "net weight": ["MaterialNetWeight", "NetWeight"],
    "weight": ["MaterialGrossWeight", "MaterialNetWeight", "GrossWeight", "NetWeight"],
    "order quantity": ["OrderQuantity", "ScheduleLineOrderQuantity"],
    "equipment": ["Equipment"],
    "manufacturing order": ["ManufacturingOrder"],
    "serial": ["SerialNumber"],
    "confirmation": ["Confirmation", "MfgOrderConfirmation"],
}


def _is_count_only(query: str) -> bool:
    """Return True when the query is a simple total-count with no grouping."""
    q = query.strip().lower()

    # Must match at least one count pattern
    matched = any(re.search(p, q) for p in _COUNT_TOTAL_PATTERNS)
    if not matched:
        return False

    # Must NOT contain grouping signals
    has_negative = any(re.search(p, q) for p in _COUNT_TOTAL_NEGATIVES)
    return not has_negative


def _extract_requested_columns(query: str, available_columns: List[str]) -> Optional[List[str]]:
    """
    If the query requests specific columns, return the resolved column names.
    Matches against available_columns (case-insensitive).
    Returns None when no specific columns are detectable.
    """
    q = query.strip()

    # Try each pattern
    for pat in _COL_SELECT_PATTERNS:
        m = re.search(pat, q, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1)
        # Strip trailing entity/prepositional clauses e.g. "of all products", "from Customers"
        raw = re.sub(r"\s+(?:of|from|for|in|where|with)\s+.*$", "", raw, flags=re.IGNORECASE).strip()
        # Split on commas and "and"
        parts = re.split(r",\s*|\s+and\s+", raw, flags=re.IGNORECASE)
        candidate_names = []
        for part in parts:
            name = part.strip().strip(".")
            # Remove trailing noise words
            name = re.sub(r"\s+(column|field|attribute)s?$", "", name, flags=re.IGNORECASE).strip()
            # Remove leading noise words
            name = re.sub(r"^(the|a|an|only|just)\s+", "", name, flags=re.IGNORECASE).strip()
            if name.lower() not in _COL_STOP_WORDS and len(name) >= 2:
                candidate_names.append(name)

        if not candidate_names:
            continue

            # Resolve against actual available columns (case-insensitive)
        if available_columns:
            avail_lower = {c.lower(): c for c in available_columns}
            resolved = []
            for name in candidate_names:
                name_clean = re.sub(r"[^a-zA-Z0-9]", "", name.lower())
                if not name_clean:
                    continue
                # Exact match (case-insensitive)
                exact = avail_lower.get(name.lower()) or avail_lower.get(name_clean)
                if exact:
                    if exact not in resolved:
                        resolved.append(exact)
                    continue
                # Partial match — column contains candidate or candidate contains column
                matches = []
                for lower, orig in avail_lower.items():
                    lower_clean = re.sub(r"[^a-zA-Z0-9]", "", lower)
                    if name_clean in lower_clean or lower_clean in name_clean:
                        matches.append(orig)
                if len(matches) == 1:
                    if matches[0] not in resolved:
                        resolved.append(matches[0])
                elif len(matches) > 1:
                    # Prefer shortest (most specific) match
                    best_match = min(matches, key=len)
                    if best_match not in resolved:
                        resolved.append(best_match)
                else:
                    # Fuzzy: try common synonyms and word splitting
                    # e.g., "order number" → "PurchaseOrder", "serial number" → "SerialNumber"
                    words = re.findall(r"[a-z]+", name.lower())
                    # Try synonym lookup first
                    matched_synonym = False
                    for syn_key, syn_targets in _COL_SYNONYMS.items():
                        if syn_key in name.lower():
                            for target in syn_targets:
                                target_lower = target.lower()
                                for lower, orig in avail_lower.items():
                                    if target_lower == lower or target_lower in lower or lower in target_lower:
                                        if orig not in resolved:
                                            resolved.append(orig)
                                            matched_synonym = True
                                            break
                                if matched_synonym:
                                    break
                        if matched_synonym:
                            break
                    if not matched_synonym:
                        for lower, orig in avail_lower.items():
                            lower_words = re.findall(r"[a-z]+", lower.lower())
                            # Check if all user words appear in column name (as substrings)
                            if all(any(w in lw for lw in lower_words) for w in words if len(w) > 2):
                                if orig not in resolved:
                                    resolved.append(orig)
                                    break
            if resolved:
                return resolved
        else:
            # No available_columns provided — return raw names as-is
            return candidate_names if candidate_names else None

    return None


def detect_query_intent(
    query: str,
    available_columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Analyse the query and return a structured intent dict:

    Count-total intent:
      {"type": "count_total"}

    Column-select intent:
      {"type": "column_select", "columns": ["ProductName", "UnitPrice"]}

    No special intent:
      {"type": None}
    """
    q = query.strip()

    # 0. "show all X" / "list all X" — means all columns, no column_select
    if re.match(r"^(?:show|list|get|display|fetch)\s+(?:me\s+)?(?:all\s+)", q, re.IGNORECASE):
        return {"type": None}

    # 1. Total count?
    if _is_count_only(q):
        logger.debug(f"QueryIntentDetector: count_total detected for '{q[:60]}'")
        return {"type": "count_total"}

    # 2. Specific column selection?
    requested_cols = _extract_requested_columns(q, available_columns or [])
    if requested_cols:
        logger.debug(f"QueryIntentDetector: column_select detected — {requested_cols}")
        return {"type": "column_select", "columns": requested_cols}

    return {"type": None}
