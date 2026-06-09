"""
Data cleaning module for ML pipelines.
Handles missing values, outliers, encoding, normalization, and duplicates.
"""
import numpy as np
from typing import Any, Dict, List, Optional, Tuple


def _classify_columns(rows: List[Dict], columns: List[str]) -> Dict[str, Dict]:
    """Classify columns by type and detect issues."""
    info = {}
    for col in columns:
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v is not None and v != ""]
        null_count = len(values) - len(non_null)

        # Try to detect type
        is_numeric = False
        is_bool = False
        is_date = False
        unique_vals = list(set(str(v) for v in non_null))

        if non_null:
            sample = non_null[:50]
            numeric_count = sum(1 for v in sample if _is_numeric(v))
            bool_count = sum(1 for v in sample if isinstance(v, bool) or str(v).lower() in ("true", "false", "0", "1"))
            date_count = sum(1 for v in sample if _is_date_like(str(v)))

            if numeric_count > len(sample) * 0.8:
                is_numeric = True
            elif bool_count > len(sample) * 0.8:
                is_bool = True
            elif date_count > len(sample) * 0.8:
                is_date = True

        info[col] = {
            "type": "numeric" if is_numeric else "bool" if is_bool else "date" if is_date else "categorical",
            "null_count": null_count,
            "null_pct": round(null_count / max(len(values), 1) * 100, 1),
            "unique_count": len(unique_vals),
            "is_constant": len(unique_vals) <= 1,
            "needs_encoding": not is_numeric and not is_bool and len(unique_vals) <= 50,
        }
    return info


def _is_numeric(v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    try:
        float(str(v))
        return True
    except (ValueError, TypeError):
        return False


def _is_date_like(s: str) -> bool:
    if not s:
        return False
    indicators = ["-", "/", "T", ":", "date", "time"]
    return any(ind in s.lower() for ind in indicators)


def clean_data(
    rows: List[Dict],
    columns: List[str],
    options: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Clean the data and return cleaned rows + report.

    Options:
      - handle_missing: "drop" | "mean" | "median" | "mode" | "zero" (default: "drop")
      - remove_outliers: bool (default: False)
      - outlier_method: "zscore" | "iqr" (default: "zscore")
      - outlier_threshold: float (default: 3.0)
      - normalize: bool (default: False)
      - normalize_method: "minmax" | "zscore" (default: "minmax")
      - remove_duplicates: bool (default: True)
      - encode_categorical: bool (default: True)
    """
    if not options:
        options = {}

    handle_missing = options.get("handle_missing", "drop")
    remove_outliers = options.get("remove_outliers", False)
    outlier_method = options.get("outlier_method", "zscore")
    outlier_threshold = options.get("outlier_threshold", 3.0)
    do_normalize = options.get("normalize", False)
    normalize_method = options.get("normalize_method", "minmax")
    remove_dupes = options.get("remove_duplicates", True)
    encode_cat = options.get("encode_categorical", True)

    col_info = _classify_columns(rows, columns)
    original_count = len(rows)
    report = {
        "original_rows": original_count,
        "original_columns": len(columns),
        "column_info": col_info,
        "steps": [],
    }

    # Work on copies
    cleaned_rows = [dict(r) for r in rows]

    # 1. Remove duplicates
    if remove_dupes:
        seen = set()
        deduped = []
        for r in cleaned_rows:
            key = tuple(sorted(r.items()))
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        dupes_removed = len(cleaned_rows) - len(deduped)
        cleaned_rows = deduped
        if dupes_removed > 0:
            report["steps"].append({"step": "remove_duplicates", "removed": dupes_removed})

    # 2. Handle missing values
    if handle_missing != "keep":
        numeric_cols = [c for c in columns if col_info[c]["type"] == "numeric"]
        cat_cols = [c for c in columns if col_info[c]["type"] == "categorical"]

        if handle_missing == "drop":
            before = len(cleaned_rows)
            cleaned_rows = [r for r in cleaned_rows if all(r.get(c) is not None and r.get(c) != "" for c in columns)]
            dropped = before - len(cleaned_rows)
            if dropped > 0:
                report["steps"].append({"step": "drop_missing", "dropped": dropped})

        elif handle_missing in ("mean", "median", "mode", "zero"):
            for col in columns:
                values = [r.get(col) for r in cleaned_rows if r.get(col) is not None and r.get(col) != ""]
                if not values:
                    continue

                if col in numeric_cols and handle_missing in ("mean", "median"):
                    nums = [_to_float(v) for v in values if _to_numeric(v)]
                    if nums:
                        fill = np.mean(nums) if handle_missing == "mean" else np.median(nums)
                        fill = float(fill)
                    else:
                        fill = 0.0
                elif handle_missing == "zero":
                    fill = 0.0 if col in numeric_cols else ""
                else:  # mode
                    from collections import Counter
                    counts = Counter(str(v) for v in values)
                    fill = counts.most_common(1)[0][0]
                    if col in numeric_cols:
                        fill = _to_float(fill)

                filled = 0
                for r in cleaned_rows:
                    if r.get(col) is None or r.get(col) == "":
                        r[col] = fill
                        filled += 1
                if filled > 0:
                    report["steps"].append({"step": f"fill_{handle_missing}", "column": col, "filled": filled, "value": fill})

    # 3. Remove outliers
    if remove_outliers:
        numeric_cols = [c for c in columns if col_info[c]["type"] == "numeric"]
        for col in numeric_cols:
            nums = [_to_float(r.get(col)) for r in cleaned_rows if _to_numeric(r.get(col))]
            if len(nums) < 3:
                continue
            mean = np.mean(nums)
            _std = float(np.std(nums))
            std = _std if _std > 0 else 1.0
            q1, q3 = np.percentile(nums, [25, 75])
            iqr = q3 - q1

            before = len(cleaned_rows)
            filtered = []
            for r in cleaned_rows:
                v = _to_float(r.get(col))
                if not _to_numeric(r.get(col)):
                    filtered.append(r)
                    continue
                if outlier_method == "zscore":
                    if std > 0 and abs((v - mean) / std) <= outlier_threshold:
                        filtered.append(r)
                else:  # iqr
                    if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr:
                        filtered.append(r)
            removed = before - len(filtered)
            cleaned_rows = filtered
            if removed > 0:
                report["steps"].append({"step": "remove_outliers", "column": col, "removed": removed, "method": outlier_method})

    # 4. Normalize
    if do_normalize:
        numeric_cols = [c for c in columns if col_info[c]["type"] == "numeric"]
        for col in numeric_cols:
            nums = [_to_float(r.get(col)) for r in cleaned_rows if _to_numeric(r.get(col))]
            if not nums:
                continue
            if normalize_method == "minmax":
                mn, mx = min(nums), max(nums)
                rng = mx - mn if mx != mn else 1.0
                for r in cleaned_rows:
                    if _to_numeric(r.get(col)):
                        r[col] = round((_to_float(r[col]) - mn) / rng, 6)
            else:  # zscore
                mean = np.mean(nums)
                std = np.std(nums) if np.std(nums) > 0 else 1.0
                for r in cleaned_rows:
                    if _to_numeric(r.get(col)):
                        r[col] = round((_to_float(r[col]) - mean) / std, 6)
            report["steps"].append({"step": "normalize", "column": col, "method": normalize_method})

    # 5. Encode categorical
    if encode_cat:
        cat_cols = [c for c in columns if col_info[c]["type"] == "categorical" and not col_info[c]["is_constant"]]
        new_cols = list(columns)
        for col in cat_cols:
            unique_vals = sorted(set(str(r.get(col, "")) for r in cleaned_rows))
            if len(unique_vals) < 2 or len(unique_vals) > 50:
                continue
            for val in unique_vals:
                new_col = f"{col}_{val}"
                if new_col not in new_cols:
                    new_cols.append(new_col)
                for r in cleaned_rows:
                    r[new_col] = 1 if str(r.get(col, "")) == val else 0
            report["steps"].append({"step": "encode", "column": col, "categories": len(unique_vals)})

        # Remove original categorical columns
        for col in cat_cols:
            if col in new_cols:
                new_cols.remove(col)
            for r in cleaned_rows:
                r.pop(col, None)
        columns = new_cols

    report["final_rows"] = len(cleaned_rows)
    report["final_columns"] = len(columns)

    return {
        "columns": columns,
        "rows": cleaned_rows,
        "report": report,
    }


def _to_numeric(v) -> bool:
    if v is None or v == "":
        return False
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(str(v))
        return True
    except (ValueError, TypeError):
        return False


def _to_float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0
