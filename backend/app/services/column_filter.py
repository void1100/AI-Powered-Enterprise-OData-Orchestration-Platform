"""Post-fetch column filtering.

The default view should reduce noise without hiding business-critical SAP
fields. SAP services often return sparse or constant values in small result
sets, so the filter is intentionally conservative for low row counts.
"""
import re
from typing import Any, Dict, List, Set


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


def filter_columns(
    table: Dict[str, Any],
    user_hidden: Set[str] = None,
    keep_hints: List[str] = None,
) -> Dict[str, Any]:
    """Filter a table while preserving business-relevant columns.

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

    keep_cols = []
    hidden_columns = []

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

    filtered_rows = [{k: row.get(k, "") for k in keep_cols} for row in rows]
    hidden_names = {c["name"] for c in hidden_columns}
    smart_columns = [c for c in (table.get("smart_columns") or []) if c not in hidden_names]
    smart_rows = None
    if smart_columns:
        smart_source_rows = table.get("smart_rows") or rows
        smart_rows = [{k: row.get(k, "") for k in smart_columns} for row in smart_source_rows]

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
        "filter_mode": "business_safe",
        "filter_note": (
            "Small result set: business columns are preserved; only technical columns are hidden."
            if small_result
            else "Business-safe filter applied; technical/noisy columns hidden."
        ),
    }
