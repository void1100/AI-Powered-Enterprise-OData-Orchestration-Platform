"""ML router — unsupervised analysis, supervised training, prediction, data cleaning, and OData pagination."""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from app.auth import get_current_user

router = APIRouter(tags=["ml"])


@router.post("/analyze")
async def analyze_table(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to analyze")
    from app.services.ml_engine import analyze_table as ml_analyze
    try:
        result = ml_analyze(table)
        return result
    except Exception as e:
        logger.error(f"ML analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@router.post("/ml/clean")
async def ml_clean(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    options = payload.get("options", {})
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to clean")
    from app.services.data_cleaner import clean_data
    try:
        result = clean_data(table["rows"], table["columns"], options)
        return result
    except Exception as e:
        logger.error(f"Data cleaning failed: {e}")
        raise HTTPException(status_code=500, detail=f"Cleaning failed: {str(e)}")


@router.post("/ml/train")
async def ml_train(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    table = payload.get("table")
    target_col = payload.get("target_column")
    algorithm = payload.get("algorithm", "random_forest")
    options = payload.get("options", {})
    compare = payload.get("compare", False)
    if not table or not table.get("rows"):
        raise HTTPException(status_code=400, detail="No table data to train on")
    if not target_col:
        raise HTTPException(status_code=400, detail="target_column is required")
    from app.services.ml_supervised import train_model, train_and_compare
    try:
        if compare:
            algorithms = payload.get("algorithms", [
                "decision_tree", "random_forest", "linear_regression",
                "logistic_regression", "xgboost", "gradient_boosting",
            ])
            result = train_and_compare(table["rows"], table["columns"], target_col, algorithms)
        else:
            result = train_model(table["rows"], table["columns"], target_col, algorithm, options)
        model_obj = result.pop("_model", None)
        if model_obj and not compare:
            from app.services.model_store import model_store
            entity_key = f"manual_{target_col}"
            model_store.store(
                entity_key=entity_key,
                model_obj=model_obj,
                feature_columns=result.get("feature_columns", []),
                target_column=target_col,
                task_type=result.get("task_type", "regression"),
                metrics=result.get("metrics", {}),
                feature_importance=result.get("feature_importance", []),
                algorithm=algorithm,
                sample_count=result.get("sample_count", 0),
            )
        return result
    except Exception as e:
        logger.error(f"ML training failed: {e}")
        raise HTTPException(status_code=500, detail=f"Training failed: {str(e)}")


@router.get("/ml/algorithms")
async def ml_algorithms():
    from app.services.ml_supervised import ALGORITHMS
    return {"algorithms": ALGORITHMS}


@router.get("/ml/models")
async def ml_models():
    from app.services.model_store import model_store
    return {"models": model_store.list_models()}


@router.post("/ml/predict")
async def ml_predict(payload: Dict[str, Any], request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    from app.services.model_store import model_store
    entity_key = payload.get("entity_key")
    features = payload.get("features", {})
    if not entity_key:
        raise HTTPException(status_code=400, detail="entity_key required")
    result = model_store.predict(entity_key, features)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No trained model for '{entity_key}'. Query the data first to train a model.",
        )
    return result


@router.post("/odata/paginate")
async def odata_paginate(payload: Dict[str, Any]):
    """Initialize pagination for a large dataset query."""
    import httpx
    from app.services.pagination import pagination_manager
    from app.services.response_sanitizer import sanitize

    url = payload.get("url")
    session_id = payload.get("session_id")
    page_size = payload.get("page_size", 50)

    if not url or not session_id:
        raise HTTPException(status_code=400, detail="url and session_id required")

    try:
        count_url = url + ("&" if "?" in url else "?") + "$count=true&$top=0"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(count_url)
            resp.raise_for_status()
            data = resp.json()
            total_count = data.get("@odata.count", 0)

        pagination_info = pagination_manager.create_session(
            session_id=session_id,
            base_url=url,
            total_count=total_count,
            page_size=page_size,
        )

        skip, top = pagination_manager.get_skip_top(session_id)
        page_url = url + ("&" if "?" in url else "?") + f"$skip={skip}&$top={top}"

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(page_url)
            resp.raise_for_status()
            raw = resp.json()

        sanitized = sanitize(raw, max_rows=top)
        return {"pagination": pagination_info, "table": sanitized}
    except Exception as e:
        logger.error(f"Pagination init failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/odata/page")
async def odata_page(payload: Dict[str, Any]):
    """Get next/previous page of paginated data."""
    import httpx
    from app.services.pagination import pagination_manager
    from app.services.response_sanitizer import sanitize

    session_id = payload.get("session_id")
    action = payload.get("action", "next")
    page = payload.get("page", 1)

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")

    state = pagination_manager.get_session(session_id)
    if not state:
        raise HTTPException(
            status_code=404,
            detail="Pagination session not found. Query again to start pagination.",
        )

    if action == "next":
        pagination_info = pagination_manager.next_page(session_id)
    elif action == "prev":
        pagination_info = pagination_manager.prev_page(session_id)
    elif action == "goto":
        pagination_info = pagination_manager.goto_page(session_id, page)
    else:
        raise HTTPException(status_code=400, detail="action must be next, prev, or goto")

    if not pagination_info:
        raise HTTPException(status_code=400, detail="No more pages available")

    try:
        skip, top = pagination_manager.get_skip_top(session_id)
        page_url = state.base_url + ("&" if "?" in state.base_url else "?") + f"$skip={skip}&$top={top}"

        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(page_url)
            resp.raise_for_status()
            raw = resp.json()

        sanitized = sanitize(raw, max_rows=top)
        return {"pagination": pagination_info, "table": sanitized}
    except Exception as e:
        logger.error(f"Pagination page failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
