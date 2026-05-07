"""Aggregation helpers for the user accounting dashboard.

The LiteLLM proxy exposes raw per-request spend logs (``/spend/logs``)
and a user record with budget metadata (``/user/info``). The dashboard
wants higher-level rollups: spend per model, per key, per project/task,
plus time-series and request-volume tallies.

This module is the single place where those rollups are computed so the
HTTP layer (``app/routes/dashboard.py``) stays thin and the math is
unit-testable in isolation.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Tag schema
# ---------------------------------------------------------------------------
#
# The dashboard surfaces a Project / Task hierarchy derived from
# ``request_tags``. Tags are expected to look like:
#
#   "project:1042"   or   "Project: 1042"
#   "task:3.1.2"     or   "Task: 3.1.2"
#
# - Project values are pure positive integers.
# - Task values are dotted positive integers in one of three forms:
#   ``dd``, ``dd.dd``, ``dd.dd.dd`` (i.e. up to three components).
#
# A request that carries a Task tag without a Project tag is treated as
# malformed and rolled into the "Unattributed" bucket so users notice
# their tagging hygiene gap.

_PROJECT_RE = re.compile(r"^\s*project\s*[:=]\s*(\d+)\s*$", re.IGNORECASE)
_TASK_RE = re.compile(
    r"^\s*task\s*[:=]\s*(\d{1,4}(?:\.\d{1,4}){0,2})\s*$", re.IGNORECASE
)

UNATTRIBUTED = "Unattributed"


def _parse_tags(raw: Any) -> tuple[str | None, str | None]:
    """Extract (project, task) from a request_tags value.

    ``raw`` may be a list of strings, a JSON-encoded list, ``None``, or
    something else entirely; we accept whatever LiteLLM hands us and
    return ``(None, None)`` when we can't make sense of it. Only the
    *first* well-formed Project / Task tag wins — repeats are ignored
    rather than rejected, since tag noise is common in practice.
    """
    if raw is None:
        return None, None
    if isinstance(raw, str):
        # Some LiteLLM responses serialize request_tags as a JSON string.
        # Try to recover; if parsing fails, treat the whole string as a
        # single tag so users still get *something* back.
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                raw = parsed
            else:
                raw = [raw]
        except (ValueError, TypeError):
            raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None, None

    project: str | None = None
    task: str | None = None
    for tag in raw:
        if not isinstance(tag, str):
            continue
        if project is None:
            m = _PROJECT_RE.match(tag)
            if m:
                project = m.group(1)
                continue
        if task is None:
            m = _TASK_RE.match(tag)
            if m:
                task = m.group(1)
    return project, task


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    """Parse a LiteLLM timestamp into a tz-aware datetime, or ``None``.

    LiteLLM hands back ISO-8601 strings, occasionally with a trailing
    ``Z`` suffix. ``datetime.fromisoformat`` only accepts ``Z`` from
    Python 3.11+, so we normalise just in case.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Date-only ("2026-05-07") still works with fromisoformat in
        # 3.11+, but if anything else slips through we just drop it.
        try:
            dt = datetime.fromisoformat(s + "T00:00:00+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty list."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    rank = max(0, min(len(s) - 1, int(math.ceil(pct / 100.0 * len(s))) - 1))
    return float(s[rank])


def _safe_div(num: float, denom: float) -> float | None:
    if not denom:
        return None
    return num / denom


# ---------------------------------------------------------------------------
# Status / success classification
# ---------------------------------------------------------------------------
#
# The spend log row doesn't carry a status_code field directly. We pull
# whatever we can out of the metadata, which LiteLLM populates with at
# least ``status`` (success|failure) on most deployments and sometimes a
# numeric ``status_code``. When neither is present we fall back to a
# heuristic: a request that recorded any spend or token usage is treated
# as a success.


def _classify_status(row: dict) -> tuple[bool, str]:
    """Return ``(success, status_code)`` for a spend log row.

    ``status_code`` is rendered as a string so the frontend can use it
    as a dict key without losing leading zeros or the literal "error"
    bucket for rows where the proxy didn't capture a code.
    """
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    status_code = metadata.get("status_code") or metadata.get("response_code")
    raw_status = (metadata.get("status") or "").lower()

    if isinstance(status_code, (int, float)) and not isinstance(status_code, bool):
        code_int = int(status_code)
        return code_int < 400, str(code_int)
    if isinstance(status_code, str) and status_code.isdigit():
        return int(status_code) < 400, status_code
    if raw_status in {"success", "ok"}:
        return True, "200"
    if raw_status in {"failure", "failed", "error"}:
        return False, "error"

    # Heuristic fallback: assume success if we have spend or tokens.
    spend = float(row.get("spend") or 0)
    tokens = int(row.get("total_tokens") or 0)
    if spend > 0 or tokens > 0:
        return True, "200"
    return False, "error"


# ---------------------------------------------------------------------------
# Budget posture
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
# Main aggregation
# ---------------------------------------------------------------------------


def _key_label(row: dict, alias_map: dict[str, str]) -> tuple[str, str]:
    """Return a stable (id, display_label) for the row's API key.

    ``alias_map`` is keyed by full token and falls back to the masked
    prefix LiteLLM returns in ``/key/list``. The display label is the
    user-supplied alias when we have it, otherwise the token prefix so
    the UI shows *something* recognisable.
    """
    api_key = row.get("api_key") or ""
    alias = alias_map.get(api_key)
    if not alias and api_key:
        # Try the masked-prefix variant ("sk-abc12345...") that
        # /key/list returns.
        masked = api_key[:8] + "..."
        alias = alias_map.get(masked)
    label = alias or (api_key[:8] + "..." if api_key else "(unknown)")
    return api_key or label, label


def _bucket_period(rows: Iterable[dict], days: int) -> list[dict]:
    """Filter rows to the trailing ``days`` window using their startTime."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for r in rows:
        dt = _parse_dt(r.get("startTime") or r.get("start_time"))
        if dt is not None and dt >= cutoff:
            out.append(r)
    return out


def _totals(rows: list[dict]) -> dict:
    spend = 0.0
    prompt = 0
    completion = 0
    total = 0
    requests = 0
    success = 0
    fail = 0
    for r in rows:
        spend += float(r.get("spend") or 0)
        prompt += int(r.get("prompt_tokens") or 0)
        completion += int(r.get("completion_tokens") or 0)
        total += int(r.get("total_tokens") or 0)
        requests += 1
        ok, _ = _classify_status(r)
        if ok:
            success += 1
        else:
            fail += 1
    return {
        "spend": round(spend, 6),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "requests": requests,
        "successful_requests": success,
        "failed_requests": fail,
        "error_rate": round(fail / requests, 4) if requests else 0.0,
    }


def _time_series(rows: list[dict]) -> list[dict]:
    """Daily buckets covering the rows' span. Empty days are filled with zeros.

    We bucket on the request's start time in UTC. Filling in the empty
    days keeps the frontend chart honest — a flat zero is more
    informative than a missing point that the chart library would
    silently smooth over.
    """
    by_day: dict[date, dict] = {}
    min_d: date | None = None
    max_d: date | None = None
    for r in rows:
        dt = _parse_dt(r.get("startTime") or r.get("start_time"))
        if dt is None:
            continue
        d = dt.astimezone(timezone.utc).date()
        bucket = by_day.setdefault(
            d,
            {"spend": 0.0, "tokens": 0, "requests": 0},
        )
        bucket["spend"] += float(r.get("spend") or 0)
        bucket["tokens"] += int(r.get("total_tokens") or 0)
        bucket["requests"] += 1
        if min_d is None or d < min_d:
            min_d = d
        if max_d is None or d > max_d:
            max_d = d

    if min_d is None or max_d is None:
        return []

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


def _per_dim(
    rows: list[dict],
    key_fn,
) -> list[dict]:
    """Group rows by an arbitrary key and compute per-group rollups."""
    buckets: dict[Any, dict] = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        b = buckets.setdefault(
            k,
            {
                "spend": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
            },
        )
        b["spend"] += float(r.get("spend") or 0)
        b["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        b["completion_tokens"] += int(r.get("completion_tokens") or 0)
        b["total_tokens"] += int(r.get("total_tokens") or 0)
        b["requests"] += 1
    out = []
    for k, b in buckets.items():
        out.append(
            {
                "key": k,
                "spend": round(b["spend"], 6),
                "prompt_tokens": b["prompt_tokens"],
                "completion_tokens": b["completion_tokens"],
                "total_tokens": b["total_tokens"],
                "requests": b["requests"],
                "cost_per_token": _safe_div(b["spend"], b["total_tokens"]),
            }
        )
    out.sort(key=lambda x: x["spend"], reverse=True)
    return out


def _projects(rows: list[dict]) -> tuple[list[dict], dict]:
    """Aggregate spend by Project, with Task drill-downs.

    Returns ``(projects, unattributed)``. A Task tag without a Project
    tag rolls into the unattributed bucket so users notice the missing
    Project tag rather than seeing a phantom Project named after the
    bare task identifier.
    """
    projects: dict[str, dict] = {}
    unattributed = {
        "spend": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }

    for r in rows:
        project, task = _parse_tags(r.get("request_tags"))
        spend = float(r.get("spend") or 0)
        prompt = int(r.get("prompt_tokens") or 0)
        completion = int(r.get("completion_tokens") or 0)
        total = int(r.get("total_tokens") or 0)

        if project is None:
            unattributed["spend"] += spend
            unattributed["prompt_tokens"] += prompt
            unattributed["completion_tokens"] += completion
            unattributed["total_tokens"] += total
            unattributed["requests"] += 1
            continue

        p = projects.setdefault(
            project,
            {
                "project": project,
                "spend": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
                "tasks": defaultdict(
                    lambda: {
                        "spend": 0.0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "requests": 0,
                    }
                ),
            },
        )
        p["spend"] += spend
        p["prompt_tokens"] += prompt
        p["completion_tokens"] += completion
        p["total_tokens"] += total
        p["requests"] += 1

        # Tasks without a value are still attributed to the project, but
        # rendered under a placeholder so users can see how much of a
        # project's spend is missing a task tag.
        task_label = task or UNATTRIBUTED
        t = p["tasks"][task_label]
        t["spend"] += spend
        t["prompt_tokens"] += prompt
        t["completion_tokens"] += completion
        t["total_tokens"] += total
        t["requests"] += 1

    out: list[dict] = []
    for p in projects.values():
        tasks_list = []
        for tname, t in p["tasks"].items():
            tasks_list.append(
                {
                    "task": tname,
                    "spend": round(t["spend"], 6),
                    "prompt_tokens": t["prompt_tokens"],
                    "completion_tokens": t["completion_tokens"],
                    "total_tokens": t["total_tokens"],
                    "requests": t["requests"],
                }
            )
        tasks_list.sort(key=lambda x: x["spend"], reverse=True)
        out.append(
            {
                "project": p["project"],
                "spend": round(p["spend"], 6),
                "prompt_tokens": p["prompt_tokens"],
                "completion_tokens": p["completion_tokens"],
                "total_tokens": p["total_tokens"],
                "requests": p["requests"],
                "tasks": tasks_list,
            }
        )
    out.sort(key=lambda x: x["spend"], reverse=True)

    return out, {
        "spend": round(unattributed["spend"], 6),
        "prompt_tokens": unattributed["prompt_tokens"],
        "completion_tokens": unattributed["completion_tokens"],
        "total_tokens": unattributed["total_tokens"],
        "requests": unattributed["requests"],
    }


def _status_codes(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        _, code = _classify_status(r)
        counts[code] += 1
    return dict(counts)


def _tokens_per_request(rows: list[dict]) -> dict:
    samples = [int(r.get("total_tokens") or 0) for r in rows]
    samples = [s for s in samples if s > 0]
    if not samples:
        return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "avg": round(sum(samples) / len(samples), 2),
        "p50": round(_percentile(samples, 50), 2),
        "p95": round(_percentile(samples, 95), 2),
    }


def _build_alias_map(keys_list: list[dict]) -> dict[str, str]:
    """Map both the full token and the masked prefix to the key alias.

    LiteLLM's ``/key/list`` returns masked tokens (``sk-abc12345...``),
    while ``/spend/logs`` rows reference the *full* api_key. We index by
    both so spend rows from either source resolve to the same alias.
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


def aggregate(
    *,
    user_info: dict | None,
    spend_logs: list[dict],
    keys_list: list[dict] | None = None,
    period_days: int = 30,
) -> dict:
    """Build the full dashboard payload.

    ``period_days`` is the trailing window used for the "current period"
    rollups. Defaults to 30 to match the most common LiteLLM
    budget_duration; the frontend can re-fetch with a different value if
    the user picks a different range.
    """
    rows = list(spend_logs or [])
    keys_list = keys_list or []
    alias_map = _build_alias_map(keys_list)

    period_rows = _bucket_period(rows, period_days)

    projects, unattributed = _projects(rows)
    period_projects, period_unattributed = _projects(period_rows)

    def _key_id(r):
        return _key_label(r, alias_map)[0]

    def _key_alias(r):
        return _key_label(r, alias_map)[1]

    keys_breakdown = _per_dim(rows, _key_id)
    # Decorate the keys breakdown with display aliases.
    for entry in keys_breakdown:
        token = entry["key"]
        # _key_id returns the api_key when present; fall back to label.
        alias = alias_map.get(token) or alias_map.get(token[:8] + "...") or token
        entry["alias"] = alias
        entry["key_prefix"] = (token[:8] + "...") if token.startswith("sk-") else token

    return {
        "period_days": period_days,
        "budget": build_budget(user_info),
        "lifetime": _totals(rows),
        "current_period": _totals(period_rows),
        "tokens_per_request": _tokens_per_request(rows),
        "tokens_per_request_period": _tokens_per_request(period_rows),
        "time_series": _time_series(rows),
        "models": _per_dim(rows, lambda r: r.get("model") or "(unknown)"),
        "keys": keys_breakdown,
        "status_codes": _status_codes(rows),
        "projects": projects,
        "unattributed": unattributed,
        "current_period_projects": period_projects,
        "current_period_unattributed": period_unattributed,
        "tag_schema": {
            "project": "project:<integer>  (e.g. project:1042)",
            "task": "task:<dotted-integer>  (e.g. task:3.1.2; up to 3 components)",
            "note": (
                "Tasks must accompany a Project tag. Requests with no Project "
                "tag are surfaced as Unattributed."
            ),
        },
    }
