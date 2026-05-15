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

    payload = aggregate(
        user_info=user_info,
        daily_activity=daily_activity,
        keys_list=keys_list,
        period_days=period_days,
    )
    payload["user_email"] = user_email
    return payload
