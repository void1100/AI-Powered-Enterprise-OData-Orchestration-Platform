"""
Supervised ML module with multiple algorithms.
Supports: Decision Tree, Random Forest, XGBoost, CatBoost, Logistic Regression,
           KNN, SVM, Gradient Boosting, Ada Boost, Extra Trees, Naive Bayes.
"""
import numpy as np
from typing import Any, Dict, List, Optional, Tuple


def _sanitize(obj):
    """Recursively convert numpy types to native Python for JSON serialization."""
    if isinstance(obj, dict):
        return {_sanitize_key(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_sanitize(v) for v in obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _sanitize_key(k):
    """Convert numpy keys to native Python."""
    if isinstance(k, (np.integer,)):
        return int(k)
    if isinstance(k, (np.floating,)):
        return float(k)
    return k


def _to_float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _is_numeric(v) -> bool:
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


ALGORITHMS = {
    "decision_tree": "Decision Tree",
    "random_forest": "Random Forest",
    "xgboost": "XGBoost",
    "catboost": "CatBoost",
    "logistic_regression": "Logistic Regression",
    "linear_regression": "Linear Regression",
    "knn": "K-Nearest Neighbors",
    "svm": "Support Vector Machine",
    "gradient_boosting": "Gradient Boosting",
    "ada_boost": "Ada Boost",
    "extra_trees": "Extra Trees",
    "naive_bayes": "Naive Bayes",
}


def get_algorithm(name: str):
    """Return the sklearn/xgboost/catboost model class for the given algorithm name."""
    if name == "decision_tree":
        from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
        return DecisionTreeClassifier, DecisionTreeRegressor
    elif name == "random_forest":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        return RandomForestClassifier, RandomForestRegressor
    elif name == "xgboost":
        from xgboost import XGBClassifier, XGBRegressor
        return XGBClassifier, XGBRegressor
    elif name == "catboost":
        from catboost import CatBoostClassifier, CatBoostRegressor
        return CatBoostClassifier, CatBoostRegressor
    elif name == "logistic_regression":
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression, LogisticRegression
    elif name == "linear_regression":
        from sklearn.linear_model import LinearRegression
        from sklearn.linear_model import LinearRegression as LinearRegressionCls
        return LinearRegressionCls, LinearRegression
    elif name == "knn":
        from sklearn.neighbors import KNeighborsClassifier, KNeighborsRegressor
        return KNeighborsClassifier, KNeighborsRegressor
    elif name == "svm":
        from sklearn.svm import SVC, SVR
        return SVC, SVR
    elif name == "gradient_boosting":
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        return GradientBoostingClassifier, GradientBoostingRegressor
    elif name == "ada_boost":
        from sklearn.ensemble import AdaBoostClassifier, AdaBoostRegressor
        return AdaBoostClassifier, AdaBoostRegressor
    elif name == "extra_trees":
        from sklearn.ensemble import ExtraTreesClassifier, ExtraTreesRegressor
        return ExtraTreesClassifier, ExtraTreesRegressor
    elif name == "naive_bayes":
        from sklearn.naive_bayes import GaussianNB
        return GaussianNB, GaussianNB
    else:
        raise ValueError(f"Unknown algorithm: {name}")


def _prepare_features(
    rows: List[Dict],
    feature_cols: List[str],
    target_col: str,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Extract X (features) and y (target) as numpy arrays."""
    X_data = []
    y_data = []

    # Filter to only numeric feature columns
    numeric_features = []
    for c in feature_cols:
        vals = [r.get(c) for r in rows]
        num_count = sum(1 for v in vals if _is_numeric(v))
        if num_count > len(vals) * 0.5:
            numeric_features.append(c)

    for r in rows:
        x = []
        for c in numeric_features:
            v = r.get(c)
            x.append(_to_float(v) if _is_numeric(v) else 0.0)
        X_data.append(x)

        y_val = r.get(target_col)
        y_data.append(y_val)

    return np.array(X_data, dtype=np.float64), np.array(y_data), numeric_features


def _detect_task_type(y: np.ndarray) -> str:
    """Detect if this is classification or regression."""
    # String or object arrays are always classification
    if y.dtype.kind in ('U', 'S', 'O'):
        return "classification"
    unique = np.unique(y[~np.isnan(y)] if y.dtype == float else y)
    # Only treat as classification if very few unique integer values (like Yes/No, 0/1/2)
    if len(unique) <= 5:
        if all(isinstance(v, (int, np.integer)) or (isinstance(v, float) and v == int(v)) for v in unique):
            return "classification"
    return "regression"


def _encode_target(y: np.ndarray) -> Tuple[np.ndarray, Optional[Dict]]:
    """Encode string targets to integers for classification. Always 0-based."""
    unique = sorted(set(y))
    mapping = {v: i for i, v in enumerate(unique)}
    encoded = np.array([mapping.get(v, 0) for v in y])
    return encoded, mapping


def train_model(
    rows: List[Dict],
    columns: List[str],
    target_col: str,
    algorithm: str = "random_forest",
    options: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Train a model and return metrics + model info.

    Returns:
        {
            "algorithm": str,
            "task_type": "classification" | "regression",
            "metrics": {...},
            "feature_importance": [...],
            "confusion_matrix": [...],
            "classification_report": {...},
            "target_mapping": {...} | null,
            "feature_columns": [...],
            "sample_count": int,
        }
    """
    if not options:
        options = {}

    feature_cols = [c for c in columns if c != target_col and c not in ("@odata.etag",)]
    X, y_raw, feat_names = _prepare_features(rows, feature_cols, target_col)

    if len(X) < 2:
        return {"error": "Need at least 2 rows for training"}

    task_type = _detect_task_type(y_raw)
    target_mapping = None

    if task_type == "classification":
        y, target_mapping = _encode_target(y_raw)
        # Remove classes with only 1 sample
        unique, counts = np.unique(y, return_counts=True)
        mask = np.isin(y, unique[counts >= 2])
        X = X[mask]
        y = y[mask]
        if len(unique) < 2:
            return {"error": "Need at least 2 classes with 2+ samples each"}
    else:
        y = y_raw.astype(float)
        mask = ~np.isnan(y)
        X = X[mask]
        y = y[mask]

    ClassifierClass, RegressorClass = get_algorithm(algorithm)

    if task_type == "classification":
        model = ClassifierClass(**options)
    else:
        model = RegressorClass(**options)

    model.fit(X, y)

    # Predictions
    y_pred = model.predict(X)

    # Metrics
    metrics = {}
    if task_type == "classification":
        from sklearn.metrics import (
            accuracy_score, precision_score, recall_score, f1_score,
            confusion_matrix, classification_report
        )
        metrics = {
            "accuracy": round(float(accuracy_score(y, y_pred)), 4),
            "precision": round(float(precision_score(y, y_pred, average="weighted", zero_division=0)), 4),
            "recall": round(float(recall_score(y, y_pred, average="weighted", zero_division=0)), 4),
            "f1": round(float(f1_score(y, y_pred, average="weighted", zero_division=0)), 4),
        }
        cm = [[int(v) for v in row] for row in confusion_matrix(y, y_pred).tolist()]
        cr_raw = classification_report(y, y_pred, output_dict=True, zero_division=0)
        cr = {}
        for k, v in cr_raw.items():
            if isinstance(v, dict):
                cr[k] = {kk: float(vv) for kk, vv in v.items()}
            else:
                cr[k] = float(v) if isinstance(v, (int, float)) else v
    else:
        from sklearn.metrics import (
            mean_absolute_error, mean_squared_error, r2_score
        )
        metrics = {
            "mae": round(float(mean_absolute_error(y, y_pred)), 4),
            "mse": round(float(mean_squared_error(y, y_pred)), 4),
            "rmse": round(float(np.sqrt(mean_squared_error(y, y_pred))), 4),
            "r2": round(float(r2_score(y, y_pred)), 4),
        }
        cm = None
        cr = None

    # Feature importance
    feat_importance = []
    if hasattr(model, "feature_importances_"):
        importances = model.feature_importances_
        sorted_idx = np.argsort(importances)[::-1]
        feat_importance = [
            {"column": feat_names[i], "importance": round(float(importances[i]), 4)}
            for i in sorted_idx[:20]
        ]
    elif hasattr(model, "coef_"):
        coefs = np.abs(model.coef_)
        if coefs.ndim > 1:
            coefs = coefs.mean(axis=0)
        sorted_idx = np.argsort(coefs)[::-1]
        feat_importance = [
            {"column": feat_names[i], "importance": round(float(coefs[i]), 4)}
            for i in sorted_idx[:20]
        ]

    # Confusion matrix labels for classification
    cm_labels = None
    if task_type == "classification" and target_mapping:
        cm_labels = [k for k, v in sorted(target_mapping.items(), key=lambda x: x[1])]

    result = _sanitize({
        "algorithm": ALGORITHMS.get(algorithm, algorithm),
        "algorithm_key": algorithm,
        "task_type": task_type,
        "metrics": metrics,
        "feature_importance": feat_importance,
        "confusion_matrix": cm,
        "confusion_matrix_labels": cm_labels,
        "classification_report": cr,
        "target_mapping": target_mapping,
        "feature_columns": feat_names,
        "sample_count": len(X),
        "target_column": target_col,
    })
    # Store model separately (not JSON serializable)
    result["_model"] = model
    return result


def train_and_compare(
    rows: List[Dict],
    columns: List[str],
    target_col: str,
    algorithms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Train multiple algorithms and compare results."""
    if not algorithms:
        algorithms = ["decision_tree", "random_forest", "linear_regression", "logistic_regression"]

    results = {}
    for algo in algorithms:
        try:
            result = train_model(rows, columns, target_col, algo)
            if "error" not in result:
                results[algo] = result
        except Exception as e:
            results[algo] = {"error": str(e)}

    # Find best algorithm
    best_algo = None
    best_score = -1
    for algo, res in results.items():
        if "error" in res:
            continue
        if res["task_type"] == "classification":
            score = res["metrics"].get("accuracy", 0)
        else:
            score = res["metrics"].get("r2", 0)
        if score > best_score:
            best_score = score
            best_algo = algo

    return _sanitize({
        "results": results,
        "best_algorithm": best_algo,
        "best_score": round(best_score, 4) if best_score >= 0 else None,
        "algorithms_tested": len([r for r in results.values() if "error" not in r]),
    })
