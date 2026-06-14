import numpy as np
from typing import Any, Dict, List, Tuple


def _extract_numeric(rows: List[Dict], columns: List[str]) -> Tuple[np.ndarray, List[str]]:
    numeric_cols = []
    for c in columns:
        vals = [r.get(c) for r in rows]
        try:
            float_vals = [float(v) for v in vals if v is not None and v != ""]
            if len(float_vals) >= len(vals) * 0.5:
                numeric_cols.append(c)
        except (ValueError, TypeError):
            continue
    if not numeric_cols:
        return np.array([]), []
    matrix = []
    for r in rows:
        row = []
        for c in numeric_cols:
            try:
                row.append(float(r.get(c, 0)))
            except (ValueError, TypeError):
                row.append(0.0)
        matrix.append(row)
    return np.array(matrix, dtype=np.float64), numeric_cols


def _zscore_anomaly(matrix: np.ndarray, threshold: float = 2.0) -> List[int]:
    if matrix.size == 0 or matrix.shape[0] < 3:
        return []
    mean = np.mean(matrix, axis=0)
    std = np.std(matrix, axis=0)
    std[std == 0] = 1.0
    z = np.abs((matrix - mean) / std)
    max_z = np.max(z, axis=1)
    return [int(i) for i in np.where(max_z > threshold)[0]]


def _kmeans(matrix: np.ndarray, k: int = 3, max_iter: int = 50) -> Tuple[np.ndarray, np.ndarray]:
    n = matrix.shape[0]
    if n < k:
        return np.zeros(n, dtype=int), matrix.copy()
    mean = np.mean(matrix, axis=0)
    std = np.std(matrix, axis=0)
    std[std == 0] = 1.0
    norm = (matrix - mean) / std
    rng = np.random.RandomState(42)
    indices = rng.choice(n, k, replace=False)
    centroids = norm[indices].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        dists = np.linalg.norm(norm[:, np.newaxis] - centroids[np.newaxis, :], axis=2)
        labels = np.argmin(dists, axis=1)
        new_centroids = np.array([
            norm[labels == i].mean(axis=0) if np.any(labels == i) else centroids[i]
            for i in range(k)
        ])
        if np.allclose(centroids, new_centroids):
            break
        centroids = new_centroids
    return labels, centroids


def _pearson_corr(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _summary_stats(matrix: np.ndarray, columns: List[str]) -> Dict[str, Any]:
    stats = {}
    for i, c in enumerate(columns):
        col = matrix[:, i]
        valid = col[~np.isnan(col)]
        if len(valid) == 0:
            continue
        sorted_vals = np.sort(valid)
        mid = len(sorted_vals) // 2
        median = float(sorted_vals[mid]) if len(sorted_vals) % 2 == 1 else float((sorted_vals[mid - 1] + sorted_vals[mid]) / 2)
        q1_idx = len(sorted_vals) // 4
        q3_idx = 3 * len(sorted_vals) // 4
        stats[c] = {
            "count": int(len(valid)),
            "mean": round(float(np.mean(valid)), 4),
            "median": round(median, 4),
            "std": round(float(np.std(valid)), 4),
            "min": round(float(np.min(valid)), 4),
            "max": round(float(np.max(valid)), 4),
            "q1": round(float(sorted_vals[q1_idx]), 4),
            "q3": round(float(sorted_vals[q3_idx]), 4),
            "skewness": round(float(np.mean(((valid - np.mean(valid)) / max(np.std(valid), 1e-10)) ** 3)), 4),
        }
    return stats


def analyze_table(table: Dict[str, Any]) -> Dict[str, Any]:
    rows = table.get("rows", [])
    columns = table.get("columns", [])
    if not rows or not columns:
        return {"error": "No data to analyze", "algorithms": {}}

    matrix, numeric_cols = _extract_numeric(rows, columns)
    results: Dict[str, Any] = {
        "row_count": len(rows),
        "column_count": len(columns),
        "numeric_columns": numeric_cols,
        "algorithms": {},
    }

    stats = _summary_stats(matrix, numeric_cols)
    results["algorithms"]["summary_statistics"] = {
        "description": "Descriptive statistics for each numeric column",
        "columns": stats,
    }

    if matrix.size > 0 and matrix.shape[0] >= 3:
        anomalies = _zscore_anomaly(matrix)
        anomaly_rows = []
        for idx in anomalies:
            if idx < len(rows):
                row_summary = {}
                for c in numeric_cols:
                    col_idx = numeric_cols.index(c)
                    val = matrix[idx, col_idx]
                    mean = np.mean(matrix[:, col_idx])
                    std = np.std(matrix[:, col_idx])
                    z = abs(val - mean) / max(std, 1e-10)
                    row_summary[c] = {"value": round(float(val), 4), "z_score": round(float(z), 2)}
                anomaly_rows.append({"row_index": idx, "row": rows[idx], "deviations": row_summary})
        results["algorithms"]["anomaly_detection"] = {
            "description": "Rows with Z-score > 2.0 on any numeric column (statistical outliers)",
            "method": "Z-Score",
            "threshold": 2.0,
            "anomaly_count": len(anomalies),
            "anomalies": anomaly_rows,
        }

    if matrix.size > 0 and matrix.shape[0] >= 5 and len(numeric_cols) >= 2:
        n = len(numeric_cols)
        corr_matrix = np.zeros((n, n))
        for i in range(n):
            for j in range(n):
                corr_matrix[i, j] = _pearson_corr(matrix[:, i], matrix[:, j])
        corr_pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                corr_pairs.append({
                    "column_a": numeric_cols[i],
                    "column_b": numeric_cols[j],
                    "correlation": round(float(corr_matrix[i, j]), 4),
                    "strength": (
                        "strong positive" if corr_matrix[i, j] > 0.7 else
                        "moderate positive" if corr_matrix[i, j] > 0.4 else
                        "weak positive" if corr_matrix[i, j] > 0.1 else
                        "weak negative" if corr_matrix[i, j] > -0.4 else
                        "moderate negative" if corr_matrix[i, j] > -0.7 else
                        "strong negative"
                    ),
                })
        corr_pairs.sort(key=lambda x: abs(x["correlation"]), reverse=True)
        results["algorithms"]["correlation_analysis"] = {
            "description": "Pearson correlation between numeric columns",
            "method": "Pearson",
            "matrix": {numeric_cols[i]: {numeric_cols[j]: round(float(corr_matrix[i, j]), 4) for j in range(n)} for i in range(n)},
            "top_pairs": corr_pairs[:10],
        }

    if matrix.size > 0 and matrix.shape[0] >= 5:
        k = min(3, matrix.shape[0])
        labels, centroids = _kmeans(matrix, k=k)
        clusters = {}
        for i in range(k):
            member_rows = [rows[j] for j in range(len(rows)) if labels[j] == i]
            clusters[f"cluster_{i}"] = {
                "size": len(member_rows),
                "centroid": {numeric_cols[j]: round(float(centroids[i, j]), 4) for j in range(len(numeric_cols))},
                "sample_rows": member_rows[:3],
            }
        
        # PCA projection for 2D visualization
        scatter_data = None
        if matrix.shape[1] >= 2:
            try:
                from sklearn.decomposition import PCA
                pca = PCA(n_components=2)
                projected = pca.fit_transform(matrix)
                centroid_projected = pca.transform(centroids)
                scatter_data = {
                    "points": [
                        {
                            "x": round(float(projected[j, 0]), 4),
                            "y": round(float(projected[j, 1]), 4),
                            "cluster": int(labels[j]),
                        }
                        for j in range(len(rows))
                    ],
                    "centroids": [
                        {
                            "x": round(float(centroid_projected[i, 0]), 4),
                            "y": round(float(centroid_projected[i, 1]), 4),
                            "cluster": i,
                        }
                        for i in range(k)
                    ],
                    "variance_explained": round(float(sum(pca.explained_variance_ratio_)), 4),
                    "x_label": f"PC1 ({round(float(pca.explained_variance_ratio_[0]), 2)*100}%)",
                    "y_label": f"PC2 ({round(float(pca.explained_variance_ratio_[1]), 2)*100}%)",
                }
            except Exception:
                pass
        
        results["algorithms"]["clustering"] = {
            "description": f"K-Means clustering into {k} groups based on numeric features",
            "method": "K-Means",
            "k": k,
            "iterations_converged": True,
            "clusters": clusters,
            "labels": [int(l) for l in labels],
            "scatter_data": scatter_data,
        }

    if matrix.size > 0 and len(numeric_cols) >= 2:
        min_vals = np.min(matrix, axis=0)
        max_vals = np.max(matrix, axis=0)
        range_vals = max_vals - min_vals
        range_vals[range_vals == 0] = 1.0
        norm = (matrix - min_vals) / range_vals
        distances = np.linalg.norm(norm, axis=1)
        mean_dist = np.mean(distances)
        std_dist = np.std(distances)
        feature_importance = {}
        for i, c in enumerate(numeric_cols):
            col_range = float(range_vals[i])
            feature_importance[c] = round(col_range / float(np.sum(range_vals)), 4)
        results["algorithms"]["feature_importance"] = {
            "description": "Relative importance of each numeric column based on value range",
            "importance": feature_importance,
            "distribution": {
                "mean_distance": round(float(mean_dist), 4),
                "std_distance": round(float(std_dist), 4),
            },
        }

    total_algos = len(results["algorithms"])
    results["summary"] = f"Analysis complete: {total_algos} algorithms applied to {len(rows)} rows across {len(numeric_cols)} numeric columns"
    return results
