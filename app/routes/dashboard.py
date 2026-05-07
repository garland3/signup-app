"""User accounting dashboard route.

Pulls usage and spend data from the LiteLLM proxy, aggregates it
server-side via ``app.core.dashboard_metrics.aggregate``, and returns a
single JSON payload the frontend can render without any further math.

Aggregation is deliberately done on the server: LiteLLM's per-user
spend log can run to thousands of rows, and we don't want every page
load to ship that volume of raw data to the browser. The route also
gives us a single place to enforce ownership (the user only ever sees
their own spend logs and key aliases).
"""

import logging

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
      - ``/user/info``   for budget posture
      - ``/spend/logs``  for raw per-request spend (filtered by user_id)
      - ``/key/list``    so the per-key breakdown can show aliases

    A failure on ``/user/info`` or ``/key/list`` is non-fatal: those
    endpoints contribute *labels* and *budget metadata* but the spend
    rollups still work without them. Only a failed ``/spend/logs`` call
    surfaces as a 502 to the caller, since without it the dashboard has
    no data to render.
    """
    user_email = request.state.user_email
    client = _get_client()

    try:
        spend_logs = await client.get_spend_logs(user_id=user_email)
    except Exception as e:
        raise _upstream_error("spend_logs", e)

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
        spend_logs=spend_logs,
        keys_list=keys_list,
        period_days=period_days,
    )
    payload["user_email"] = user_email
    return payload
