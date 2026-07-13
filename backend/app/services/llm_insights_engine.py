"""LLM Insights Engine — generates data insights, ML recommendations,
and follow-up suggestions using the LLM based on data profile."""
import json
from typing import Any, Dict, List, Optional
from loguru import logger


async def generate_insights(
    profile: Dict[str, Any],
    user_query: str,
    table_data: Dict[str, Any],
    provider: str = "auto",
) -> Dict[str, Any]:
    """Generate insights about the data using the LLM.

    Returns:
        {
            "insights": [str],           # 3-5 data insights
            "suggestions": [str],        # 3-5 follow-up query suggestions
            "ml_recommendation": {...},  # ML model recommendation
            "chart_insights": [str],     # Chart-related insights
            "summary": str,             # NL summary of the data
        }
    """
    from app.config import settings
    from app.services.data_profiler import profile_for_llm

    profile_text = profile_for_llm(profile)
    sample_rows = (table_data.get("rows") or [])[:5]
    columns = table_data.get("columns", [])

    system_prompt = """You are a data analyst AI. Given a data profile and sample rows from a database query,
provide insights about the data. Be specific, actionable, and concise.

Return a JSON object with these fields:
- "insights": array of 3-5 key insights about the data (distributions, patterns, anomalies, quality issues)
- "suggestions": array of 3-5 follow-up queries the user might want to try next
- "ml_recommendation": object with "algorithm", "target_column", "task_type", and "reason" fields
- "chart_insights": array of 1-3 chart recommendations based on the data patterns
- "summary": a 1-2 sentence summary of what this data shows

Rules:
- Insights should be data-driven (reference actual numbers, columns, patterns)
- Suggestions should be specific to the data (use actual column/entity names)
- ML recommendation should pick the best algorithm for this specific data
- Be concise — each insight/suggestion should be 1 sentence max
- If the data has issues (missing values, outliers, skew), mention them"""

    user_content = f"""User query: {user_query}

Data Profile:
{profile_text}

Sample rows (first 5):
{json.dumps(sample_rows, default=str, indent=2)}

Columns: {columns[:20]}"""

    try:
        result = await _call_llm(system_prompt, user_content, provider, settings)
        return result
    except Exception as e:
        logger.warning(f"LLM insights generation failed: {e}")
        return _fallback_insights(profile, user_query)


async def _call_llm(system_prompt: str, user_content: str, provider: str, settings) -> Dict[str, Any]:
    """Call the LLM using the configured provider."""
    from app.agents.reasoning_engine import llm_engine

    # Use the same provider routing as the main chat
    provider_lower = (provider or settings.llm_provider or "mock").lower()

    if provider_lower == "nvidia" and settings.nvidia_api_key:
        return await _call_nvidia(system_prompt, user_content, settings)
    elif provider_lower in ("openai", "groq") and settings.openai_api_keys_list:
        return await _call_openai(system_prompt, user_content, settings)
    elif provider_lower == "openrouter" and settings.openrouter_api_key:
        return await _call_openrouter(system_prompt, user_content, settings)
    else:
        # Try available providers in order
        if settings.openai_api_keys_list:
            return await _call_openai(system_prompt, user_content, settings)
        elif settings.nvidia_api_key:
            return await _call_nvidia(system_prompt, user_content, settings)
        else:
            raise ValueError("No LLM provider available for insights")


async def _call_openai(system_prompt: str, user_content: str, settings) -> Dict[str, Any]:
    """Call OpenAI-compatible API (Groq)."""
    from openai import AsyncOpenAI

    keys = settings.openai_api_keys_list
    client = AsyncOpenAI(
        api_key=keys[0],
        base_url=settings.openai_base_url or None,
        timeout=30.0,
    )
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


async def _call_nvidia(system_prompt: str, user_content: str, settings) -> Dict[str, Any]:
    """Call NVIDIA API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.nvidia_api_key,
        base_url=settings.nvidia_base_url,
        timeout=30.0,
    )
    resp = await client.chat.completions.create(
        model=settings.nvidia_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


async def _call_openrouter(system_prompt: str, user_content: str, settings) -> Dict[str, Any]:
    """Call OpenRouter API."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=30.0,
    )
    resp = await client.chat.completions.create(
        model=settings.openrouter_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=2048,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


def _fallback_insights(profile: Dict[str, Any], user_query: str) -> Dict[str, Any]:
    """Generate basic insights without LLM (fallback when no provider available)."""
    insights = []
    suggestions = []

    row_count = profile.get("row_count", 0)
    col_count = profile.get("column_count", 0)
    numeric = profile.get("numeric_columns", [])
    categorical = profile.get("categorical_columns", [])

    insights.append(f"Query returned {row_count} rows with {col_count} columns ({len(numeric)} numeric, {len(categorical)} categorical)")

    if profile.get("quality_score", 100) < 80:
        insights.append(f"Data quality score is {profile['quality_score']}/100 — check for missing values or outliers")

    if profile.get("correlations"):
        top = profile["correlations"][0]
        insights.append(f"Strong {top['direction']} correlation ({top['correlation']}) between {top['columns'][0]} and {top['columns'][1]}")

    if profile.get("outlier_summary"):
        cols_with_outliers = list(profile["outlier_summary"].keys())[:3]
        insights.append(f"Outliers detected in: {', '.join(cols_with_outliers)}")

    target = profile.get("target_recommendation")
    if target:
        insights.append(f"Recommended ML target: {target['column']} ({target['task_type']})")

    # Generate basic suggestions
    if categorical:
        suggestions.append(f"Count records by {categorical[0]}")
    if numeric:
        suggestions.append(f"Show distribution of {numeric[0]}")
    suggestions.append(f"Show top 10 records by {numeric[0] if numeric else categorical[0] if categorical else 'ID'}")

    return {
        "insights": insights[:5],
        "suggestions": suggestions[:5],
        "ml_recommendation": {
            "algorithm": "random_forest",
            "target_column": target["column"] if target else None,
            "task_type": target["task_type"] if target else "regression",
            "reason": "Default recommendation (LLM unavailable)",
        },
        "chart_insights": ["Bar chart recommended for categorical analysis"] if categorical else [],
        "summary": f"Data contains {row_count} rows across {col_count} columns.",
    }


def auto_select_algorithm(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Select the best algorithm based on data profile (no LLM needed).

    Rules:
    - Small dataset (<50 rows): decision_tree or logistic_regression
    - Classification with <5 classes: random_forest or xgboost
    - Classification with many classes: gradient_boosting
    - Regression with normal distribution: linear_regression
    - Regression with skewed data: random_forest or gradient_boosting
    - Many features: random_forest (handles high dimensionality)
    """
    target = profile.get("target_recommendation")
    if not target:
        return {"algorithm": "random_forest", "reason": "No suitable target column found"}

    task_type = target["task_type"]
    n_rows = profile.get("row_count", 0)
    n_numeric = len(profile.get("numeric_columns", []))
    distributions = profile.get("distributions", {})
    target_col = target["column"]
    target_dist = distributions.get(target_col, {})

    if task_type == "classification":
        class_count = target.get("class_count", 2)
        if n_rows < 30:
            algo = "decision_tree"
            reason = f"Small dataset ({n_rows} rows) — Decision Tree avoids overfitting"
        elif class_count <= 5 and n_rows < 200:
            algo = "random_forest"
            reason = f"Balanced classification ({class_count} classes, {n_rows} rows) — Random Forest is robust"
        else:
            algo = "gradient_boosting"
            reason = f"Complex classification ({class_count} classes) — Gradient Boosting handles complexity"
    else:  # regression
        skewness = abs(target_dist.get("skewness", 0))
        if skewness < 0.5 and n_numeric <= 5:
            algo = "linear_regression"
            reason = f"Normal distribution (skew={skewness:.2f}) — Linear Regression is interpretable"
        elif n_rows < 50:
            algo = "decision_tree"
            reason = f"Small dataset ({n_rows} rows) — Decision Tree avoids overfitting"
        elif skewness > 1.0:
            algo = "random_forest"
            reason = f"Skewed distribution (skew={skewness:.2f}) — Random Forest handles non-linearity"
        else:
            algo = "gradient_boosting"
            reason = f"Moderate complexity — Gradient Boosting provides best accuracy"

    return {"algorithm": algo, "reason": reason, "task_type": task_type, "target_column": target_col}
