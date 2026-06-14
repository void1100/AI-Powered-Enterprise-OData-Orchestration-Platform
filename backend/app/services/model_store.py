"""
In-memory model store for trained ML models.
Keeps models available for prediction queries.
"""
import time
from typing import Any, Dict, List, Optional
from loguru import logger


class ModelStore:
    def __init__(self, max_models: int = 20, ttl_seconds: int = 3600):
        self._models: Dict[str, Dict[str, Any]] = {}
        self._max_models = max_models
        self._ttl = ttl_seconds

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._models.items() if now - v["trained_at"] > self._ttl]
        for k in expired:
            del self._models[k]
            logger.info(f"Model store: expired model {k}")

    def store(
        self,
        entity_key: str,
        model_obj: Any,
        feature_columns: List[str],
        target_column: str,
        task_type: str,
        metrics: Dict[str, Any],
        feature_importance: List[Dict],
        algorithm: str,
        sample_count: int,
    ):
        self._cleanup()
        if len(self._models) >= self._max_models:
            oldest = min(self._models, key=lambda k: self._models[k]["trained_at"])
            del self._models[oldest]

        self._models[entity_key] = {
            "model": model_obj,
            "feature_columns": feature_columns,
            "target_column": target_column,
            "task_type": task_type,
            "metrics": metrics,
            "feature_importance": feature_importance,
            "algorithm": algorithm,
            "sample_count": sample_count,
            "trained_at": time.time(),
        }
        logger.info(f"Model stored: {entity_key} ({algorithm}, {task_type}, {sample_count} samples)")

    def get(self, entity_key: str) -> Optional[Dict[str, Any]]:
        self._cleanup()
        return self._models.get(entity_key)

    def predict(self, entity_key: str, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        import numpy as np

        entry = self.get(entity_key)
        if not entry:
            return None

        model = entry["model"]
        feat_cols = entry["feature_columns"]

        x = []
        for col in feat_cols:
            val = features.get(col)
            if val is None:
                x.append(0.0)
            else:
                try:
                    x.append(float(val))
                except (ValueError, TypeError):
                    x.append(0.0)

        x_arr = np.array([x])
        prediction = model.predict(x_arr)
        pred_val = prediction[0]

        if hasattr(pred_val, 'item'):
            pred_val = pred_val.item()

        result = {
            "prediction": pred_val,
            "target_column": entry["target_column"],
            "task_type": entry["task_type"],
            "algorithm": entry["algorithm"],
            "model_metrics": entry["metrics"],
            "features_used": {col: features.get(col) for col in feat_cols},
        }

        if entry["task_type"] == "regression":
            result["confidence_info"] = f"Model R²={entry['metrics'].get('r2', 'N/A')}"
        else:
            result["confidence_info"] = f"Model accuracy={entry['metrics'].get('accuracy', 'N/A')}"

        return result

    def list_models(self) -> List[Dict[str, Any]]:
        self._cleanup()
        return [
            {
                "entity_key": k,
                "algorithm": v["algorithm"],
                "task_type": v["task_type"],
                "target_column": v["target_column"],
                "sample_count": v["sample_count"],
                "metrics": v["metrics"],
                "feature_columns": v["feature_columns"],
            }
            for k, v in self._models.items()
        ]

    def clear(self):
        self._models.clear()


model_store = ModelStore()
