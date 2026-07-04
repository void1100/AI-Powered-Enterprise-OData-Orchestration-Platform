"""Post-fetch column filtering — removes useless columns from query results."""
import re
from typing import Any, Dict, List, Set


# SAP internal fields that are never useful
SAP_BLACKLIST_PATTERNS = [
    r"InternalNumber$",
    r"^ProdCharc\d",
    r"ConfigurableProd",
    r"CrossPlantConfigurable",
    r"ProdCharc\dInternal",
]


def is_useless_column(col_name: str, rows: List[Dict[str, Any]], threshold: float = 0.95) -> bool:
    """Return True if a column is useless (empty, zero, or same value in >95% of rows)."""
    if not rows:
        return False

    total = len(rows)
    values = [str(row.get(col_name, "")).strip() for row in rows]

    # Check if all values are the same
    unique = set(values)
    if len(unique) <= 1:
        return True

    # Check if >threshold values are empty or zero
    empty_count = sum(1 for v in values if v in ("", "0", "0.0", "0.00", "000000000000000000000000000000", "1970-01-01T00:00:00.000"))
    if empty_count / total >= threshold:
        return True

    return False


def is_sap_blacklisted(col_name: str) -> bool:
    """Return True if column matches SAP internal field patterns."""
    for pattern in SAP_BLACKLIST_PATTERNS:
        if re.search(pattern, col_name, re.IGNORECASE):
            return True
    return False


def filter_columns(
    table: Dict[str, Any],
    user_hidden: Set[str] = None,
    keep_hints: List[str] = None,
) -> Dict[str, Any]:
    """Filter a table dict, removing useless columns.

    Args:
        table: {"columns": [...], "rows": [...], "row_count": N}
        user_hidden: Columns the user manually hid via column picker
        keep_hints: Column name substrings that should NEVER be hidden
            (e.g., ["Material", "MaterialType", "Material_Text"])
    """
    if not table or not table.get("rows"):
        return table

    columns = list(table.get("columns", []))
    rows = table["rows"]
    keep_hints = keep_hints or []
    user_hidden = user_hidden or set()

    # Determine which columns to keep
    keep_cols = []
    for col in columns:
        # User explicitly hid this column
        if col in user_hidden:
            continue
        # SAP blacklisted
        if is_sap_blacklisted(col):
            continue
        # Useless (all empty/zero/same)
        if is_useless_column(col, rows):
            continue
        keep_cols.append(col)

    # If we filtered everything, keep at least the first 3 columns
    if not keep_cols:
        keep_cols = columns[:3]

    # Build filtered rows
    filtered_rows = []
    for row in rows:
        filtered_rows.append({k: row.get(k, "") for k in keep_cols})

    return {
        "columns": keep_cols,
        "rows": filtered_rows,
        "row_count": len(filtered_rows),
        "truncated": table.get("truncated", False),
        "total_count": table.get("total_count"),
    }
