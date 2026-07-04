"""Token usage tracking — logs every LLM call for the Usage dashboard."""
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from loguru import logger

from app.config import settings

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# Token cost per million tokens (input/output)
PROVIDER_COSTS = {
    "groq": {"input": 0.59, "output": 0.79, "label": "Groq Llama 3.3 70B"},
    "nvidia": {"input": 0.0, "output": 0.0, "label": "NVIDIA Llama 3.3 70B"},
    "openai": {"input": 2.50, "output": 10.00, "label": "OpenAI GPT-4o"},
    "openrouter": {"input": 0.15, "output": 0.15, "label": "OpenRouter"},
    "gemini": {"input": 0.0, "output": 0.0, "label": "Gemini 2.0 Flash"},
    "mock": {"input": 0.0, "output": 0.0, "label": "Mock Planner"},
    "cached": {"input": 0.0, "output": 0.0, "label": "Cached"},
    "entity_join": {"input": 0.0, "output": 0.0, "label": "Entity Join"},
    "entity_select": {"input": 0.0, "output": 0.0, "label": "Entity Select"},
    "model_store": {"input": 0.0, "output": 0.0, "label": "ML Model"},
}

# Rate limits per provider (tokens per day, requests per minute)
PROVIDER_LIMITS = {
    "groq": {"daily_tokens": 500_000, "rpm": 30, "label": "Groq Free Tier"},
    "nvidia": {"daily_tokens": 1_000_000, "rpm": 20, "label": "NVIDIA Free Tier"},
    "openai": {"daily_tokens": 10_000_000, "rpm": 500, "label": "OpenAI Paid"},
    "openrouter": {"daily_tokens": 1_000_000, "rpm": 20, "label": "OpenRouter Free"},
    "gemini": {"daily_tokens": 1_000_000, "rpm": 15, "label": "Gemini Free Tier"},
    "mock": {"daily_tokens": 999_999_999, "rpm": 999, "label": "Unlimited"},
}


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_dir = os.path.dirname(settings.sqlite_db_path) or "."
        os.makedirs(db_dir, exist_ok=True)
        _conn = sqlite3.connect(settings.sqlite_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_usage (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            session_id TEXT,
            user_query TEXT,
            provider TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            intent TEXT DEFAULT '',
            cached INTEGER DEFAULT 0,
            user_role TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(timestamp);
        CREATE INDEX IF NOT EXISTS idx_token_usage_provider ON token_usage(provider, timestamp);
        CREATE INDEX IF NOT EXISTS idx_token_usage_session ON token_usage(session_id, timestamp);
        """
    )
    conn.commit()


def log_usage(
    provider: str = "unknown",
    tokens: int = 0,
    latency_ms: int = 0,
    session_id: str = "",
    user_query: str = "",
    model: str = "",
    intent: str = "",
    cached: bool = False,
    user_role: str = "",
):
    """Log a single LLM usage record."""
    now = datetime.now(timezone.utc).isoformat()
    uid = str(uuid.uuid4())
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO token_usage (id, timestamp, session_id, user_query, provider, model, tokens, latency_ms, intent, cached, user_role) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, now, session_id, user_query[:500], provider, model, tokens, latency_ms, intent, 1 if cached else 0, user_role),
        )
        conn.commit()


def get_usage_today() -> Dict[str, Any]:
    """Get today's token usage breakdown."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _get_usage_for_date(today)


def get_usage_date(date_str: str) -> Dict[str, Any]:
    """Get usage for a specific date (YYYY-MM-DD)."""
    return _get_usage_for_date(date_str)


def _get_usage_for_date(date_str: str) -> Dict[str, Any]:
    conn = _get_conn()
    # Total for the day
    cur = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) as total_tokens, COUNT(*) as total_queries FROM token_usage WHERE timestamp LIKE ?",
        (f"{date_str}%",),
    )
    row = cur.fetchone()
    total_tokens = row["total_tokens"] if row else 0
    total_queries = row["total_queries"] if row else 0

    # By provider
    cur = conn.execute(
        "SELECT provider, COALESCE(SUM(tokens), 0) as tokens, COUNT(*) as queries, COALESCE(AVG(latency_ms), 0) as avg_latency FROM token_usage WHERE timestamp LIKE ? GROUP BY provider ORDER BY tokens DESC",
        (f"{date_str}%",),
    )
    by_provider = [dict(r) for r in cur.fetchall()]

    # By hour
    cur = conn.execute(
        "SELECT CAST(strftime('%H', timestamp) AS INTEGER) as hour, COALESCE(SUM(tokens), 0) as tokens FROM token_usage WHERE timestamp LIKE ? GROUP BY hour ORDER BY hour",
        (f"{date_str}%",),
    )
    by_hour = [dict(r) for r in cur.fetchall()]

    return {
        "date": date_str,
        "total_tokens": total_tokens,
        "total_queries": total_queries,
        "by_provider": by_provider,
        "by_hour": by_hour,
    }


def get_usage_by_day(days: int = 30) -> List[Dict[str, Any]]:
    """Get daily token usage for the last N days."""
    conn = _get_conn()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cur = conn.execute(
        "SELECT DATE(timestamp) as date, COALESCE(SUM(tokens), 0) as tokens, COUNT(*) as queries FROM token_usage WHERE timestamp >= ? GROUP BY DATE(timestamp) ORDER BY date ASC",
        (start.isoformat(),),
    )
    return [dict(r) for r in cur.fetchall()]


def get_usage_by_provider() -> List[Dict[str, Any]]:
    """Get total usage by provider (all time)."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT provider, COALESCE(SUM(tokens), 0) as total_tokens, COUNT(*) as total_queries, COALESCE(AVG(latency_ms), 0) as avg_latency, MIN(timestamp) as first_used, MAX(timestamp) as last_used FROM token_usage GROUP BY provider ORDER BY total_tokens DESC"
    )
    results = []
    for r in cur.fetchall():
        d = dict(r)
        cost_info = PROVIDER_COSTS.get(d["provider"], {"input": 0, "output": 0, "label": d["provider"]})
        # Rough cost estimate (assume 60% input, 40% output)
        est_input_tokens = d["total_tokens"] * 0.6
        est_output_tokens = d["total_tokens"] * 0.4
        d["estimated_cost"] = round(
            (est_input_tokens * cost_info["input"] + est_output_tokens * cost_info["output"]) / 1_000_000, 4
        )
        d["label"] = cost_info["label"]
        results.append(d)
    return results


def get_recent_queries(limit: int = 30) -> List[Dict[str, Any]]:
    """Get recent queries with token info."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT timestamp, user_query, provider, tokens, latency_ms, intent, cached, session_id FROM token_usage ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def get_daily_average(days: int = 7) -> Dict[str, Any]:
    """Get daily average over the last N days."""
    conn = _get_conn()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cur = conn.execute(
        "SELECT COUNT(*) as days_with_data, COALESCE(SUM(daily_total), 0) as total_tokens, COALESCE(AVG(daily_total), 0) as avg_daily_tokens FROM (SELECT DATE(timestamp) as date, SUM(tokens) as daily_total FROM token_usage WHERE timestamp >= ? GROUP BY DATE(timestamp)) sub",
        (start.isoformat(),),
    )
    row = cur.fetchone()
    days_with_data = row["days_with_data"] if row else 1
    return {
        "period_days": days,
        "days_with_data": max(days_with_data, 1),
        "total_tokens": row["total_tokens"] if row else 0,
        "avg_daily_tokens": round(row["avg_daily_tokens"] if row else 0),
    }


def get_rate_limits() -> List[Dict[str, Any]]:
    """Get current rate limit status for each provider."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_conn()
    cur = conn.execute(
        "SELECT provider, COALESCE(SUM(tokens), 0) as used_tokens FROM token_usage WHERE timestamp LIKE ? GROUP BY provider",
        (f"{today}%",),
    )
    usage_by_provider = {r["provider"]: r["used_tokens"] for r in cur.fetchall()}

    limits = []
    for provider, info in PROVIDER_LIMITS.items():
        used = usage_by_provider.get(provider, 0)
        limit = info["daily_tokens"]
        pct = round((used / limit) * 100, 1) if limit > 0 else 0
        limits.append({
            "provider": provider,
            "label": info["label"],
            "daily_limit": limit,
            "used_today": used,
            "remaining": max(0, limit - used),
            "usage_percent": min(pct, 100),
            "rpm_limit": info["rpm"],
            "status": "ok" if pct < 80 else "warning" if pct < 95 else "critical",
        })
    return sorted(limits, key=lambda x: x["usage_percent"], reverse=True)


def get_usage_full() -> Dict[str, Any]:
    """Get complete usage data for the admin dashboard."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "today": get_usage_today(),
        "this_week": get_usage_by_day(7),
        "this_month": get_usage_by_day(30),
        "by_provider": get_usage_by_provider(),
        "recent_queries": get_recent_queries(30),
        "daily_average": get_daily_average(7),
        "rate_limits": get_rate_limits(),
    }
