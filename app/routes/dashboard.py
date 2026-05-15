"""User accounting dashboard route.

Pulls pre-aggregated daily activity from the LiteLLM proxy
(``/user/daily/activity`` — the same endpoint LiteLLM's admin UI uses)
and flattens it into the JSON shape the dashboard frontend renders.
``/user/info`` contributes the budget posture and ``/key/list`` supplies
alias labels for the per-key breakdown.

We deliberately use the daily-rollup endpoint instead of
``/spend/logs``: the per-request log is several orders of magnitude
larger than the daily summary, and on busy accounts it reliably trips
the upstream read timeout.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.config import get_settings
from app.core.dashboard_metrics import aggregate
from app.core.litellm_client import LiteLLMClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _get_client() -> LiteLLMClient:
    return LiteLLMClient(get_settings())


def _upstream_error(op: str, exc: Exception) -> HTTPException:
    """Mirror the keys-route convention: log details, return a generic 502."""
    logger.exception("LiteLLM %s failed: %s", op, exc)
    return HTTPException(status_code=502, detail="Upstream service error")


@router.get("/dashboard")
async def get_dashboard(
    request: Request,
    period_days: int = Query(
        30, ge=1, le=365,
        description="Trailing window in days for the 'current period' rollups",
    ),
):
    """Return the aggregated accounting dashboard payload.

    Calls three LiteLLM endpoints in sequence:
      - ``/user/daily/activity`` for the daily spend/usage rollup
      - ``/user/info``           for budget posture
      - ``/key/list``            so the per-key breakdown can show aliases

    A failure on ``/user/info`` or ``/key/list`` is non-fatal: those
    endpoints contribute *labels* and *budget metadata* but the spend
    rollups still work without them. Only a failed daily-activity call
    surfaces as a 502 to the caller, since without it the dashboard has
    no data to render.
    """
    user_email = request.state.user_email
    client = _get_client()

    if not user_email:
        # Defensive: middleware should reject unauthenticated requests
        # before we get here, but if anything ever lets an empty
        # user_email through we MUST NOT call the upstream without a
        # user_id filter — LiteLLM treats a missing user_id from an
        # admin caller as "global view" and returns every user's data.
        logger.error("dashboard: empty user_email in request state")
        raise HTTPException(status_code=401, detail="Unauthorized")

    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=period_days)).isoformat()
    end_date = today.isoformat()

    try:
        daily_activity = await client.get_user_daily_activity(
            user_id=user_email, start_date=start_date, end_date=end_date
        )
    except Exception as e:
        raise _upstream_error("user_daily_activity", e)

    user_info = None
    try:
        user_info = await client.get_user_info(user_id=user_email)
    except Exception as e:
        # Don't fail the whole dashboard for a missing user record:
        # newly-onboarded users may not have one yet.
        logger.warning("get_user_info failed for %s: %s", user_email, e)

    keys_list: list[dict] = []
    try:
        result = await client.list_keys(user_id=user_email)
        keys_list = result if isinstance(result, list) else result.get("keys", [])
    except Exception as e:
        logger.warning("list_keys failed for %s: %s", user_email, e)

    # Defensive: even though /user/daily/activity is supposed to filter
    # by user_id when an admin caller passes one, sanity-check that the
    # api_keys in the response breakdown actually belong to this user.
    # Foreign keys would indicate either a misconfigured admin role on
    # the LiteLLM side or a user_id mismatch (e.g. the spend table
    # stored the email in a different normalization than /user/info).
    # In either case we drop the foreign keys and re-derive the totals
    # so the dashboard never shows another user's spend.
    daily_activity = _scope_to_user_keys(
        daily_activity, keys_list, user_email=user_email
    )

    payload = aggregate(
        user_info=user_info,
        daily_activity=daily_activity,
        keys_list=keys_list,
        period_days=period_days,
    )
    payload["user_email"] = user_email
    return payload


_METRIC_FIELDS = (
    "spend",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "api_requests",
    "successful_requests",
    "failed_requests",
)


def _zero_metrics() -> dict:
    return {f: 0.0 if f == "spend" else 0 for f in _METRIC_FIELDS}


def _add_into(dst: dict, src: dict) -> None:
    if not isinstance(src, dict):
        return
    for f in _METRIC_FIELDS:
        v = src.get(f) or 0
        if f == "spend":
            dst[f] += float(v)
        else:
            dst[f] += int(v)


def _user_token_matchers(keys_list: list[dict]) -> tuple[set[str], set[str]]:
    """Return (full_tokens, prefix_tokens) the user is known to own.

    ``/key/list`` returns full tokens when called with
    ``return_full_object=true`` against a real LiteLLM proxy, but our
    local mock (and some older LiteLLM versions) masks them as
    ``sk-abcd1234...``. We collect both forms so the foreign-key check
    works against either backend.
    """
    full: set[str] = set()
    prefixes: set[str] = set()
    for k in keys_list:
        for field in ("token", "token_id", "key"):
            value = k.get(field)
            if not isinstance(value, str) or not value:
                continue
            if value.endswith("..."):
                prefixes.add(value[:-3])
            else:
                full.add(value)
                if len(value) >= 8:
                    prefixes.add(value[:8])
    return full, prefixes


def _belongs_to_user(
    token: str, full: set[str], prefixes: set[str]
) -> bool:
    if not isinstance(token, str) or not token:
        return False
    if token in full:
        return True
    if not prefixes:
        return False
    if token.endswith("..."):
        return token[:-3] in prefixes
    return any(token.startswith(p) for p in prefixes)


def _scope_to_user_keys(
    activity: dict, keys_list: list[dict], *, user_email: str
) -> dict:
    """Filter daily-activity rows to only the user's own api_keys.

    LiteLLM's ``/user/daily/activity`` is supposed to filter by
    ``user_id`` server-side when an admin caller passes one, but in
    practice we've seen the filter fail to apply (returning a global
    rollup instead of the user's slice). This guard cross-references
    every ``api_key`` in the breakdown against ``/key/list``: any
    token that doesn't belong to the user is excluded, and the
    day-level totals are recomputed from the surviving per-key metrics.

    The downside is that spend tied to *deleted* keys (which no longer
    appear in ``/key/list``) is excluded too. We log when that happens
    so the gap is visible.
    """
    results = activity.get("results") or []
    if not isinstance(results, list) or not results:
        return activity
    if not keys_list:
        # No way to verify ownership without a key list. Leave the
        # response as-is rather than silently zero everything out.
        return activity

    full, prefixes = _user_token_matchers(keys_list)

    foreign: set[str] = set()
    new_results: list[dict] = []
    for entry in results:
        breakdown = entry.get("breakdown") or {}
        api_keys = breakdown.get("api_keys") or {}
        if not isinstance(api_keys, dict):
            new_results.append(entry)
            continue

        kept_keys: dict[str, dict] = {}
        day_metrics = _zero_metrics()
        for token, value in api_keys.items():
            if not isinstance(value, dict):
                continue
            if not _belongs_to_user(token, full, prefixes):
                foreign.add(token[:12])
                continue
            kept_keys[token] = value
            _add_into(day_metrics, value.get("metrics") or {})

        # Rebuild the per-model breakdown from each model's
        # api_key_breakdown, keeping only the user's tokens.
        kept_models: dict[str, dict] = {}
        models_in = breakdown.get("models") or {}
        if isinstance(models_in, dict):
            for model_name, model_value in models_in.items():
                if not isinstance(model_value, dict):
                    continue
                ak = model_value.get("api_key_breakdown") or {}
                if not isinstance(ak, dict):
                    continue
                model_metrics = _zero_metrics()
                kept_any = False
                for token, sub in ak.items():
                    if not isinstance(sub, dict):
                        continue
                    if not _belongs_to_user(token, full, prefixes):
                        continue
                    kept_any = True
                    _add_into(model_metrics, sub.get("metrics") or {})
                if kept_any:
                    kept_models[model_name] = {
                        "metrics": model_metrics, "metadata": {},
                    }

        new_results.append({
            **entry,
            "metrics": day_metrics,
            "breakdown": {
                "models": kept_models,
                "api_keys": kept_keys,
                "providers": {},
            },
        })

    if foreign:
        logger.warning(
            "dashboard: dropped %d foreign api_key(s) from /user/daily/activity "
            "response for user=%s — upstream user_id filter did not scope "
            "the response",
            len(foreign), user_email,
        )

    return {**activity, "results": new_results}
