"""Data Profiler — scans fetched table data and produces a structured profile
for LLM insights and auto-ML model selection."""
import math
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger


def profile_table(rows: List[Dict], columns: List[str]) -> Dict[str, Any]:
    """Analyze a table and return a structured profile."""
    if not rows or not columns:
        return {"error": "empty_table", "row_count": 0, "column_count": 0}

    n_rows = len(rows)
    n_cols = len(columns)

    # Classify columns
    col_profiles = []
    numeric_cols = []
    categorical_cols = []
    date_cols = []
    bool_cols = []
    id_cols = []

    for col in columns:
        if col.startswith("@odata.") or col == "odata.etag":
            continue
        cp = _profile_column(col, rows)
        col_profiles.append(cp)
        if cp["detected_type"] == "numeric":
            numeric_cols.append(col)
        elif cp["detected_type"] == "categorical":
            categorical_cols.append(col)
        elif cp["detected_type"] == "date":
            date_cols.append(col)
        elif cp["detected_type"] == "bool":
            bool_cols.append(col)
        if _is_id_column(col):
            id_cols.append(col)

    # Correlations between numeric columns
    correlations = []
    if len(numeric_cols) >= 2:
        correlations = _compute_correlations(rows, numeric_cols)

    # Outlier summary
    outlier_summary = _detect_outlier_summary(rows, numeric_cols)

    # Data quality score (0-100)
    quality_score = _compute_quality_score(rows, col_profiles)

    # Target column recommendation
    target_recommendation = _recommend_target(rows, numeric_cols, categorical_cols)

    # Distribution summary for numeric columns
    distributions = {}
    for col in numeric_cols:
        vals = [float(r[col]) for r in rows if r.get(col) is not None and _is_numeric_val(r[col])]
        if vals:
            distributions[col] = _distribution_summary(vals)

    return {
        "row_count": n_rows,
        "column_count": n_cols,
        "numeric_columns": numeric_cols,
        "categorical_columns": categorical_cols,
        "date_columns": date_cols,
        "bool_columns": bool_cols,
        "id_columns": id_cols,
        "column_profiles": col_profiles,
        "correlations": correlations[:10],
        "outlier_summary": outlier_summary,
        "quality_score": quality_score,
        "target_recommendation": target_recommendation,
        "distributions": distributions,
        "has_time_series": len(date_cols) > 0 and n_rows >= 10,
    }


def _profile_column(col: str, rows: List[Dict]) -> Dict[str, Any]:
    """Profile a single column."""
    values = [r.get(col) for r in rows]
    non_null = [v for v in values if v is not None and v != ""]
    null_count = len(values) - len(non_null)
    null_pct = round(null_count / len(values) * 100, 1) if values else 0

    unique_vals = list(set(str(v) for v in non_null))
    unique_count = len(unique_vals)

    # Detect type
    detected_type = _detect_type(non_null)

    profile = {
        "name": col,
        "detected_type": detected_type,
        "null_count": null_count,
        "null_pct": null_pct,
        "unique_count": unique_count,
    }

    if detected_type == "numeric":
        nums = [float(v) for v in non_null if _is_numeric_val(v)]
        if nums:
            nums_sorted = sorted(nums)
            n = len(nums)
            profile["min"] = round(nums_sorted[0], 4)
            profile["max"] = round(nums_sorted[-1], 4)
            profile["mean"] = round(sum(nums) / n, 4)
            profile["median"] = round(nums_sorted[n // 2], 4)
            profile["std"] = round(_std_dev(nums), 4)
            profile["skewness"] = round(_skewness(nums), 4)

    elif detected_type == "categorical":
        # Top values
        from collections import Counter
        counts = Counter(str(v) for v in non_null)
        profile["top_values"] = dict(counts.most_common(5))

    return profile


def _detect_type(values: List[Any]) -> str:
    """Detect the type of a column from its values."""
    if not values:
        return "categorical"

    sample = values[:min(100, len(values))]

    # Bool check
    bool_vals = {str(v).lower() for v in sample}
    if bool_vals <= {"true", "false", "0", "1", "yes", "no"}:
        return "bool"

    # Date check
    date_indicators = ["-", "/", "T", ":", "date", "time", "year", "month"]
    date_count = sum(1 for v in sample if any(ind in str(v).lower() for ind in date_indicators))
    if date_count / len(sample) > 0.6:
        return "date"

    # Numeric check
    numeric_count = sum(1 for v in sample if _is_numeric_val(v))
    if numeric_count / len(sample) > 0.5:
        return "numeric"

    return "categorical"


def _is_numeric_val(v) -> bool:
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


def _is_id_column(col: str) -> bool:
    lower = col.lower()
    return lower.endswith("id") or lower == "id"


def _std_dev(nums: List[float]) -> float:
    n = len(nums)
    if n < 2:
        return 0.0
    mean = sum(nums) / n
    variance = sum((x - mean) ** 2 for x in nums) / (n - 1)
    return math.sqrt(variance)


def _skewness(nums: List[float]) -> float:
    n = len(nums)
    if n < 3:
        return 0.0
    mean = sum(nums) / n
    std = _std_dev(nums)
    if std == 0:
        return 0.0
    m3 = sum((x - mean) ** 3 for x in nums) / n
    return m3 / (std ** 3)


def _compute_correlations(rows: List[Dict], numeric_cols: List[str]) -> List[Dict]:
    """Compute pairwise Pearson correlations."""
    correlations = []
    for i in range(len(numeric_cols)):
        for j in range(i + 1, len(numeric_cols)):
            col_a = numeric_cols[i]
            col_b = numeric_cols[j]
            pairs = []
            for r in rows:
                va = r.get(col_a)
                vb = r.get(col_b)
                if va is not None and vb is not None and _is_numeric_val(va) and _is_numeric_val(vb):
                    pairs.append((float(va), float(vb)))
            if len(pairs) >= 5:
                corr = _pearson_correlation(pairs)
                if abs(corr) >= 0.5:
                    strength = "strong" if abs(corr) >= 0.7 else "moderate"
                    direction = "positive" if corr > 0 else "negative"
                    correlations.append({
                        "columns": [col_a, col_b],
                        "correlation": round(corr, 4),
                        "strength": strength,
                        "direction": direction,
                    })
    correlations.sort(key=lambda x: -abs(x["correlation"]))
    return correlations


def _pearson_correlation(pairs: List[Tuple[float, float]]) -> float:
    n = len(pairs)
    if n < 2:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    std_x = _std_dev(xs)
    std_y = _std_dev(ys)
    if std_x == 0 or std_y == 0:
        return 0.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / (n - 1)
    return cov / (std_x * std_y)


def _detect_outlier_summary(rows: List[Dict], numeric_cols: List[str]) -> Dict[str, Any]:
    """Detect outliers using IQR method."""
    summary = {}
    for col in numeric_cols:
        vals = sorted([float(r[col]) for r in rows if r.get(col) is not None and _is_numeric_val(r[col])])
        if len(vals) < 4:
            continue
        n = len(vals)
        q1 = vals[n // 4]
        q3 = vals[3 * n // 4]
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        outliers = [v for v in vals if v < lower or v > upper]
        if outliers:
            summary[col] = {
                "count": len(outliers),
                "pct": round(len(outliers) / n * 100, 1),
                "lower_bound": round(lower, 4),
                "upper_bound": round(upper, 4),
            }
    return summary


def _compute_quality_score(rows: List[Dict], col_profiles: List[Dict]) -> int:
    """Compute a 0-100 data quality score."""
    if not col_profiles:
        return 50

    scores = []
    for cp in col_profiles:
        # Completeness (no nulls = 100)
        completeness = 100 - cp["null_pct"]
        # Uniqueness (not too many duplicates for ID-like columns)
        uniqueness = min(100, cp["unique_count"] / max(len(rows), 1) * 100)
        scores.append((completeness + uniqueness) / 2)

    return round(sum(scores) / len(scores)) if scores else 50


def _recommend_target(
    rows: List[Dict],
    numeric_cols: List[str],
    categorical_cols: List[str],
) -> Optional[Dict[str, Any]]:
    """Recommend the best target column for ML training."""
    n_rows = len(rows)

    # Classification targets: categorical columns with 2-10 unique values
    for col in categorical_cols:
        vals = [str(r.get(col, "")) for r in rows if r.get(col) is not None]
        unique = list(set(vals))
        if 2 <= len(unique) <= 10 and all(sum(1 for v in vals if v == u) >= 3 for u in unique):
            return {
                "column": col,
                "task_type": "classification",
                "classes": unique,
                "class_count": len(unique),
                "reason": f"Categorical column with {len(unique)} balanced classes",
            }

    # Regression targets: numeric columns with >= 5 unique values
    for col in reversed(numeric_cols):
        vals = [float(r[col]) for r in rows if r.get(col) is not None and _is_numeric_val(r[col])]
        unique = list(set(vals))
        if len(unique) >= 5:
            return {
                "column": col,
                "task_type": "regression",
                "unique_values": len(unique),
                "range": [round(min(unique), 4), round(max(unique), 4)],
                "reason": f"Numeric column with {len(unique)} unique values, good for regression",
            }

    return None


def _distribution_summary(nums: List[float]) -> Dict[str, Any]:
    """Summarize distribution of numeric values."""
    n = len(nums)
    if n == 0:
        return {}
    s = sorted(nums)
    mean = sum(nums) / n
    std = _std_dev(nums)
    skew = _skewness(nums)

    # Normality heuristic: skewness near 0 and reasonable std
    is_normal = abs(skew) < 1.0 and std > 0

    return {
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "mean": round(mean, 4),
        "std": round(std, 4),
        "skewness": round(skew, 4),
        "is_normal": is_normal,
        "range": round(s[-1] - s[0], 4),
    }


def profile_for_llm(profile: Dict[str, Any]) -> str:
    """Convert a data profile to a concise text summary for LLM context."""
    if "error" in profile:
        return "Empty table with no data."

    lines = []
    lines.append(f"Table: {profile['row_count']} rows, {profile['column_count']} columns")
    lines.append(f"Numeric: {len(profile['numeric_columns'])} | Categorical: {len(profile['categorical_columns'])} | Date: {len(profile['date_columns'])} | Bool: {len(profile['bool_columns'])}")
    lines.append(f"Data quality score: {profile['quality_score']}/100")

    if profile.get("target_recommendation"):
        t = profile["target_recommendation"]
        lines.append(f"Recommended ML target: {t['column']} ({t['task_type']}, {t['reason']})")

    if profile.get("correlations"):
        top_corr = profile["correlations"][:3]
        for c in top_corr:
            lines.append(f"Correlation: {c['columns'][0]} ↔ {c['columns'][1]} = {c['correlation']} ({c['strength']} {c['direction']})")

    if profile.get("outlier_summary"):
        for col, info in list(profile["outlier_summary"].items())[:3]:
            lines.append(f"Outliers: {col} has {info['count']} outliers ({info['pct']}%)")

    if profile.get("distributions"):
        for col, dist in list(profile["distributions"].items())[:3]:
            shape = "normal-like" if dist.get("is_normal") else ("right-skewed" if dist.get("skewness", 0) > 1 else "left-skewed" if dist.get("skewness", 0) < -1 else "irregular")
            lines.append(f"Distribution: {col} is {shape} (mean={dist['mean']}, std={dist['std']})")

    return "\n".join(lines)
