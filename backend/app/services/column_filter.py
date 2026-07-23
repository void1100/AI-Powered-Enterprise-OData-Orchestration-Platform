"""Post-fetch column filtering.

The default view should reduce noise without hiding business-critical SAP
fields. SAP services often return sparse or constant values in small result
sets, so the filter is intentionally conservative for low row counts.

Column ordering is governed by the priority queue in column_priority.py:
  Rank 0 — UI-annotated fields (isLineItem / isSelectionField / importance HIGH)
  Rank 1 — Key / ID fields
  Rank 2 — Business-named fields (Name, Status, Date, Amount …)
  Rank 3 — Remaining non-noisy fields
  Rank 99 — Audit / timestamp / internal fields (hidden or last)
"""
import re
from typing import Any, Dict, List, Optional, Set


SAP_BLACKLIST_PATTERNS = [
    r"InternalNumber$",
    r"^ProdCharc\d",
    r"ConfigurableProd",
    r"CrossPlantConfigurable",
    r"ProdCharc\dInternal",
    r"^__",
    r"@odata",
    r"etag$",
]

BUSINESS_KEEP_PATTERNS = [
    r"order",
    r"manufacturingorder",
    r"purchaseorder",
    r"salesorder",
    r"operation",
    r"material",
    r"plant",
    r"companycode",
    r"supplier",
    r"customer",
    r"workcenter",
    r"status",
    r"release",
    r"currency",
    r"date",
    r"time",
    r"quantity",
    r"amount",
    r"price",
    r"cost",
    r"batch",
    r"storagelocation",
    r"purchasinggroup",
    r"purchasingorganization",
    r"description",
    r"text$",
    r"name$",
]

EMPTY_OR_DEFAULT_VALUES = {
    "",
    "None",
    "none",
    "NULL",
    "null",
    "NaN",
    "nan",
    "0",
    "0.0",
    "0.00",
    "000000000000000000000000000000",
    "1970-01-01T00:00:00.000",
}

EMPTY_VALUES = {
    "",
    "None",
    "none",
    "NULL",
    "null",
    "NaN",
    "nan",
}

SMALL_RESULT_ROW_COUNT = 20


def is_sap_blacklisted(col_name: str) -> bool:
    """Return True if column matches SAP internal field patterns."""
    return any(re.search(pattern, col_name, re.IGNORECASE) for pattern in SAP_BLACKLIST_PATTERNS)


def is_business_column(col_name: str, column_labels: Dict[str, str] = None) -> bool:
    """Return True for fields likely to matter to business users."""
    label = (column_labels or {}).get(col_name, "")
    haystack = f"{col_name} {label}".lower().replace("_", "")
    return any(re.search(pattern, haystack, re.IGNORECASE) for pattern in BUSINESS_KEEP_PATTERNS)


def is_useless_column(col_name: str, rows: List[Dict[str, Any]], threshold: float = 0.95, small_result: bool = False) -> bool:
    """Return True if a column is empty/default or same value in most rows.
    For small results, only hide truly empty columns (not constant-value ones)."""
    if not rows:
        return False

    total = len(rows)
    values = [str(row.get(col_name, "")).strip() for row in rows]

    empty_count = sum(1 for v in values if v in EMPTY_OR_DEFAULT_VALUES)
    if empty_count / total >= threshold:
        return True

    if not small_result and len(set(values)) <= 1:
        return True

    return False


def is_empty_column(col_name: str, rows: List[Dict[str, Any]]) -> bool:
    """Return True when every returned row is blank/null for this column."""
    if not rows:
        return False
    values = [str(row.get(col_name, "")).strip() for row in rows]
    return all(v in EMPTY_VALUES for v in values)


def hidden_reason(col_name: str, rows: List[Dict[str, Any]]) -> str:
    if is_sap_blacklisted(col_name):
        return "technical/internal field"
    if not rows:
        return "filtered"

    values = [str(row.get(col_name, "")).strip() for row in rows]
    if all(v in EMPTY_VALUES for v in values):
        return "empty/null in result set"
    if len(set(values)) <= 1:
        return "same value in result set"

    empty_count = sum(1 for v in values if v in EMPTY_OR_DEFAULT_VALUES)
    if empty_count / len(rows) >= 0.95:
        return "mostly empty/default values"
    return "filtered"


def _sort_by_priority(
    columns: List[str],
    column_labels: Dict[str, str],
    column_priorities: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Sort kept columns by business priority.

    If column_priorities is provided (pre-computed from column_priority.py),
    we use those scores directly.  Otherwise we fall back to the lightweight
    heuristic patterns already defined in this module.
    """
    if column_priorities:
        # Build an O(1) lookup: field_name -> position in priority list
        priority_index = {p["field"]: i for i, p in enumerate(column_priorities)}
        MAX_RANK = len(column_priorities) + 1
        return sorted(columns, key=lambda c: priority_index.get(c, MAX_RANK))

    # ── Lightweight fallback heuristic ──────────────────────────────────────
    _ID_RE = re.compile(r"(ID|Id|Code|Key)$")
    _BUSINESS_RE = re.compile(
        r"(Name|Status|Date|Amount|Price|Quantity|Material|Order|Plant"
        r"|Customer|Supplier|Company|Description|Text|Currency|Unit)$",
        re.IGNORECASE,
    )
    _AUDIT_RE = re.compile(
        r"(CreatedAt|ChangedAt|LastChanged|Timestamp|ETag|Guid|UUID|InternalNumber)$",
        re.IGNORECASE,
    )

    def _rank(col: str) -> int:
        label = (column_labels or {}).get(col, "")
        haystack = f"{col} {label}"
        if _ID_RE.search(col):
            return 1
        if _BUSINESS_RE.search(haystack):
            return 2
        if _AUDIT_RE.search(col):
            return 99
        return 3

    return sorted(columns, key=lambda c: (_rank(c), c))


def filter_columns(
    table: Dict[str, Any],
    user_hidden: Set[str] = None,
    keep_hints: List[str] = None,
    column_priorities: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Filter and priority-sort a table's columns.

    Priority queue order (low number = shown first):
      0 — SAP UI-annotated (LineItem / SelectionField / importance HIGH)
      1 — Key / ID fields
      2 — Business-named fields (Name, Status, Date, Amount …)
      3 — All other non-noisy columns
     99 — Audit / timestamp / internal fields

    Returns filtered data plus full data and hidden-column metadata so the
    frontend can offer Business View / All Columns behavior.
    """
    if not table or not table.get("rows"):
        return table

    columns = list(table.get("columns", []))
    rows = table["rows"]
    column_labels = table.get("column_labels") or {}
    user_hidden = user_hidden or set()
    keep_hints = keep_hints or []
    small_result = len(rows) < SMALL_RESULT_ROW_COUNT

    keep_cols: List[str] = []
    hidden_columns: List[Dict[str, str]] = []

    for col in columns:
        if col in user_hidden:
            hidden_columns.append({"name": col, "reason": "hidden by user"})
            continue
        if is_sap_blacklisted(col):
            hidden_columns.append({"name": col, "reason": "technical/internal field"})
            continue
        if is_empty_column(col, rows):
            hidden_columns.append({"name": col, "reason": "empty/null in result set"})
            continue
        if col in keep_hints or is_business_column(col, column_labels):
            keep_cols.append(col)
            continue
        if not small_result and is_useless_column(col, rows, small_result=small_result):
            hidden_columns.append({"name": col, "reason": hidden_reason(col, rows)})
            continue
        keep_cols.append(col)

    if not keep_cols:
        keep_cols = [c for c in columns if not is_sap_blacklisted(c)][:8] or columns[:3]

    # ── Priority-sort the kept columns ──────────────────────────────────────
    keep_cols = _sort_by_priority(keep_cols, column_labels, column_priorities)

    filtered_rows = [{k: row.get(k, "") for k in keep_cols} for row in rows]
    hidden_names = {c["name"] for c in hidden_columns}
    smart_columns = [c for c in (table.get("smart_columns") or []) if c not in hidden_names]
    smart_rows = None
    if smart_columns:
        smart_source_rows = table.get("smart_rows") or rows
        smart_rows = [{k: row.get(k, "") for k in smart_columns} for row in smart_source_rows]

    # Build per-column priority metadata for the frontend
    priority_map: Dict[str, Any] = {}
    if column_priorities:
        for p in column_priorities:
            priority_map[p["field"]] = {
                "rank": p.get("_sort_key", [3])[0] if "_sort_key" in p else 3,
                "isKey": p.get("isKey", False),
                "isLineItem": p.get("isLineItem", False),
                "importance": p.get("importance", ""),
                "label": p.get("label", ""),
            }

    return {
        "columns": keep_cols,
        "rows": filtered_rows,
        "row_count": len(filtered_rows),
        "truncated": table.get("truncated", False),
        "total_count": table.get("total_count"),
        "all_columns": columns,
        "all_rows": rows,
        "column_labels": column_labels or None,
        "hidden_columns": hidden_columns,
        "smart_columns": smart_columns or None,
        "smart_rows": smart_rows,
        "column_priorities": priority_map or None,
        "filter_mode": "priority_sorted",
        "filter_note": (
            "Small result set: business columns preserved; only technical columns hidden."
            if small_result
            else "Columns sorted by business priority; technical/noisy columns hidden."
        ),
    }
