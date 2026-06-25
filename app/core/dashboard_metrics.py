"""Aggregation helpers for the user accounting dashboard.

The proxy exposes pre-aggregated daily activity at
``/user/daily/activity``: one entry per day with per-model and
per-api_key sub-totals already rolled up. The dashboard consumes that
shape directly — there's nothing to recompute per-request because the
proxy doesn't expose per-request rows in this path.

The single place we still need to do work is:
  - Splitting "lifetime" vs. "current_period" totals when the caller
    asks for a window narrower than what we fetched. (In practice we
    fetch exactly the window the dashboard asks for, so the two are
    usually equal — but we keep the split so the JS payload contract
    is stable.)
  - Flattening the nested per-model and per-api_key breakdowns into the
    sorted lists the frontend renders.
  - Deriving the budget posture from ``/user/info``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_div(num: float, denom: float) -> float | None:
    if not denom:
        return None
    return num / denom


def _parse_date(value: Any) -> date | None:
    """Parse a ``YYYY-MM-DD`` string (or a ``date``) into a ``date``."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _zero_metrics() -> dict[str, Any]:
    return {
        "spend": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
        "successful_requests": 0,
        "failed_requests": 0,
    }


def _add_metrics(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Accumulate a LiteLLM ``SpendMetrics`` block into ``dst``.

    The upstream schema names the request count ``api_requests`` and
    splits success/failure into ``successful_requests`` /
    ``failed_requests``. We expose a flat ``requests`` field plus the
    success/failure counts so the JS rollups don't have to know about
    the upstream naming.
    """
    if not isinstance(src, dict):
        return
    dst["spend"] += float(src.get("spend") or 0)
    dst["prompt_tokens"] += int(src.get("prompt_tokens") or 0)
    dst["completion_tokens"] += int(src.get("completion_tokens") or 0)
    dst["total_tokens"] += int(src.get("total_tokens") or 0)
    dst["requests"] += int(src.get("api_requests") or 0)
    dst["successful_requests"] += int(src.get("successful_requests") or 0)
    dst["failed_requests"] += int(src.get("failed_requests") or 0)


def _finalize_metrics(m: dict[str, Any]) -> dict[str, Any]:
    requests = m["requests"]
    return {
        "spend": round(m["spend"], 6),
        "prompt_tokens": m["prompt_tokens"],
        "completion_tokens": m["completion_tokens"],
        "total_tokens": m["total_tokens"],
        "requests": requests,
        "successful_requests": m["successful_requests"],
        "failed_requests": m["failed_requests"],
        "error_rate": (
            round(m["failed_requests"] / requests, 4) if requests else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# Budget posture
# ---------------------------------------------------------------------------


def build_budget(user_info: dict | None) -> dict:
    """Project the budget-relevant fields from ``/user/info``.

    The proxy exposes max_budget, soft_budget, spend, budget_duration,
    and budget_reset_at on the user record. We forward those plus a few
    derived fields (remaining, consumed_pct, soft_threshold_hit) so the
    frontend doesn't have to re-do the arithmetic.
    """
    info = user_info or {}
    # /user/info responses sometimes nest the actual record one level down.
    if "user_info" in info and isinstance(info["user_info"], dict):
        record = info["user_info"]
    else:
        record = info

    spend = _coerce_float(record.get("spend")) or 0.0
    max_budget = _coerce_float(record.get("max_budget"))
    soft_budget = _coerce_float(record.get("soft_budget"))

    remaining: float | None = None
    consumed_pct: float | None = None
    if max_budget is not None and max_budget > 0:
        remaining = max(0.0, max_budget - spend)
        consumed_pct = min(100.0, (spend / max_budget) * 100.0)

    soft_threshold_hit = (
        soft_budget is not None and spend >= soft_budget and soft_budget > 0
    )

    return {
        "spend": round(spend, 6),
        "max_budget": max_budget,
        "soft_budget": soft_budget,
        "remaining": None if remaining is None else round(remaining, 6),
        "consumed_pct": None if consumed_pct is None else round(consumed_pct, 2),
        "budget_duration": record.get("budget_duration"),
        "budget_reset_at": record.get("budget_reset_at"),
        "soft_threshold_hit": soft_threshold_hit,
    }


# ---------------------------------------------------------------------------
# Daily activity aggregation
# ---------------------------------------------------------------------------


def _build_alias_map(keys_list: list[dict]) -> dict[str, str]:
    """Map both the full token and the masked prefix to the key alias.

    LiteLLM's ``/key/list`` returns masked tokens (``sk-abc12345...``),
    while ``/user/daily/activity`` references the *full* api_key. We
    index by both so labels resolve regardless of which form turns up.
    """
    out: dict[str, str] = {}
    for k in keys_list:
        alias = k.get("key_alias") or k.get("key_name") or ""
        if not alias:
            continue
        token = k.get("token") or k.get("key") or ""
        if token:
            out[token] = alias
        token_id = k.get("token_id")
        if token_id:
            out[token_id] = alias
    return out


def _masked_token(token: str) -> str:
    return f"{token[:8]}..."


def _build_metadata_map(keys_list: list[dict]) -> dict[str, dict]:
    """Map known token identifiers to their key metadata."""
    out: dict[str, dict] = {}
    for k in keys_list:
        metadata = k.get("metadata")
        if not isinstance(metadata, dict):
            continue
        for token in (k.get("token"), k.get("key"), k.get("token_id")):
            if token:
                out[token] = metadata
    return out


def _time_series(results: list[dict]) -> list[dict]:
    """Daily buckets straight from the proxy's daily rollup, gap-filled.

    Empty days inside the span are filled with zeros so the chart
    library doesn't silently smooth over missing data.
    """
    by_day: dict[date, dict] = {}
    for entry in results:
        d = _parse_date(entry.get("date"))
        if d is None:
            continue
        m = entry.get("metrics") or {}
        bucket = by_day.setdefault(
            d, {"spend": 0.0, "tokens": 0, "requests": 0}
        )
        bucket["spend"] += float(m.get("spend") or 0)
        bucket["tokens"] += int(m.get("total_tokens") or 0)
        bucket["requests"] += int(m.get("api_requests") or 0)

    if not by_day:
        return []
    min_d = min(by_day)
    max_d = max(by_day)
    out = []
    cur = min_d
    while cur <= max_d:
        bucket = by_day.get(cur, {"spend": 0.0, "tokens": 0, "requests": 0})
        out.append(
            {
                "date": cur.isoformat(),
                "spend": round(bucket["spend"], 6),
                "tokens": bucket["tokens"],
                "requests": bucket["requests"],
            }
        )
        cur += timedelta(days=1)
    return out


def _flatten_dim(
    results: list[dict],
    dim: str,
    *,
    extract_alias=None,
) -> list[dict]:
    """Aggregate a ``BreakdownMetrics`` dimension across the daily results.

    ``dim`` is one of ``"models"``, ``"api_keys"``, ``"providers"``.
    ``extract_alias``, if provided, is called with the per-entry dict to
    pull a display label out of nested metadata (used for ``api_keys``).
    Returns a list sorted by spend descending.
    """
    buckets: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for entry in results:
        breakdown = entry.get("breakdown") or {}
        sub = breakdown.get(dim) or {}
        if not isinstance(sub, dict):
            continue
        for key, value in sub.items():
            if not isinstance(value, dict):
                continue
            b = buckets.setdefault(key, _zero_metrics())
            _add_metrics(b, value.get("metrics") or {})
            if extract_alias is not None:
                alias = extract_alias(value)
                if alias and key not in aliases:
                    aliases[key] = alias

    out: list[dict] = []
    for key, b in buckets.items():
        finalized = _finalize_metrics(b)
        finalized["key"] = key
        finalized["cost_per_token"] = _safe_div(
            finalized["spend"], finalized["total_tokens"]
        )
        if aliases:
            finalized["alias"] = aliases.get(key, "")
        out.append(finalized)
    out.sort(key=lambda x: x["spend"], reverse=True)
    return out


def _filter_results_by_window(
    results: list[dict], period_days: int
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).date()
    out = []
    for entry in results:
        d = _parse_date(entry.get("date"))
        if d is None or d >= cutoff:
            out.append(entry)
    return out


def _totals(results: list[dict]) -> dict:
    acc = _zero_metrics()
    for entry in results:
        _add_metrics(acc, entry.get("metrics") or {})
    return _finalize_metrics(acc)


def _metadata_value(metadata: dict, field: str) -> str:
    value = metadata.get(field)
    if value is None:
        return ""
    return str(value).strip()


def _project_task_breakdown(
    results: list[dict],
    metadata_map: dict[str, dict],
) -> list[dict]:
    buckets: dict[tuple[str, str], dict] = {}
    for entry in results:
        breakdown = entry.get("breakdown") or {}
        api_keys = breakdown.get("api_keys") or {}
        if not isinstance(api_keys, dict):
            continue
        for token, value in api_keys.items():
            if not isinstance(value, dict):
                continue
            token_str = str(token)
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            fallback = metadata_map.get(token_str) or metadata_map.get(
                _masked_token(token_str)
            )
            if isinstance(fallback, dict):
                metadata = {**fallback, **metadata}
            project = _metadata_value(metadata, "project")
            task_number = _metadata_value(metadata, "task_number")
            if not project and not task_number:
                continue
            b = buckets.setdefault((project, task_number), _zero_metrics())
            _add_metrics(b, value.get("metrics") or {})

    out: list[dict] = []
    for (project, task_number), b in buckets.items():
        finalized = _finalize_metrics(b)
        finalized["project"] = project
        finalized["task_number"] = task_number
        out.append(finalized)
    out.sort(key=lambda x: x["spend"], reverse=True)
    return out


def _tokens_per_request(totals: dict) -> dict:
    """Average tokens per request — the only TPR figure recoverable.

    The proxy's daily rollup doesn't preserve per-request samples, so
    p50/p95 aren't computable. We still emit the keys so the frontend
    contract is stable; p50/p95 are reported as ``None`` and the UI
    hides the sub-label when both are absent.
    """
    avg = _safe_div(totals["total_tokens"], totals["requests"])
    return {
        "avg": round(avg, 2) if avg is not None else 0.0,
        "p50": None,
        "p95": None,
    }


def aggregate(
    *,
    user_info: dict | None,
    daily_activity: dict | None,
    keys_list: list[dict] | None = None,
    period_days: int = 30,
) -> dict:
    """Build the full dashboard payload from a ``/user/daily/activity`` response.

    ``daily_activity`` is expected to be the ``SpendAnalyticsPaginatedResponse``
    dict (with ``results`` and ``metadata``). ``period_days`` is the
    trailing window for the "current period" rollups; when the upstream
    fetch already used the same window, current_period == lifetime,
    which is fine — the frontend renders both independently.
    """
    activity = daily_activity or {}
    results = activity.get("results") or []
    if not isinstance(results, list):
        results = []

    keys_list = keys_list or []
    alias_map = _build_alias_map(keys_list)
    metadata_map = _build_metadata_map(keys_list)

    period_results = _filter_results_by_window(results, period_days)

    def _api_key_alias(entry: dict) -> str:
        metadata = entry.get("metadata")
        if isinstance(metadata, dict):
            alias = metadata.get("key_alias")
            if alias:
                return str(alias)
        return ""

    models = _flatten_dim(results, "models")
    keys_breakdown = _flatten_dim(
        results, "api_keys", extract_alias=_api_key_alias
    )
    # Decorate keys with /key/list alias as a fallback, and add the
    # masked prefix the UI renders next to the alias.
    for entry in keys_breakdown:
        token = entry["key"]
        alias = entry.get("alias") or alias_map.get(token) or alias_map.get(
            _masked_token(token)
        ) or token
        entry["alias"] = alias
        entry["key_prefix"] = (
            _masked_token(token)
            if isinstance(token, str) and token.startswith("sk-")
            else token
        )

    lifetime_totals = _totals(results)
    period_totals = _totals(period_results)

    # /user/daily/activity exposes success/failure counts but not the
    # per-status_code histogram. Project the two buckets the frontend
    # status table can render so the UI doesn't have to special-case
    # an empty histogram.
    status_codes: dict[str, int] = {}
    if lifetime_totals["successful_requests"]:
        status_codes["200"] = lifetime_totals["successful_requests"]
    if lifetime_totals["failed_requests"]:
        status_codes["error"] = lifetime_totals["failed_requests"]

    return {
        "period_days": period_days,
        "budget": build_budget(user_info),
        "lifetime": lifetime_totals,
        "current_period": period_totals,
        "tokens_per_request": _tokens_per_request(lifetime_totals),
        "tokens_per_request_period": _tokens_per_request(period_totals),
        "time_series": _time_series(results),
        "models": models,
        "keys": keys_breakdown,
        "project_task_breakdown": _project_task_breakdown(results, metadata_map),
        "status_codes": status_codes,
    }
