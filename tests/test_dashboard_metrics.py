"""Unit tests for the dashboard aggregation pipeline.

Tests focus on the rules that distinguish the dashboard from a generic
"sum of spend logs" tally:

- Project / Task tag parsing, including the malformed-task-only case
  that has to land in the unattributed bucket.
- Budget posture: derived ``remaining`` and ``consumed_pct`` plus the
  soft-threshold flag.
- Tokens-per-request percentile math.
- Time-series gap-fill.
- Status code classification (explicit metadata wins over heuristic).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.dashboard_metrics import (
    UNATTRIBUTED,
    _parse_tags,
    _percentile,
    aggregate,
    build_budget,
)


def _row(**overrides):
    """Build a spend log row with sensible defaults."""
    base = {
        "request_id": "req-test",
        "api_key": "sk-test1234567890",
        "model": "gpt-4o",
        "spend": 0.01,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "startTime": datetime.now(timezone.utc).isoformat(),
        "user": "alice@example.com",
        "metadata": {"status": "success", "status_code": 200},
        "request_tags": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


def test_parse_tags_recognises_explicit_project_and_task():
    p, t = _parse_tags(["project:1042", "task:3.1.2"])
    assert p == "1042"
    assert t == "3.1.2"


def test_parse_tags_accepts_human_friendly_form_with_spaces():
    p, t = _parse_tags(["Project: 1042", "Task: 3.1"])
    assert p == "1042"
    assert t == "3.1"


def test_parse_tags_rejects_unrecognised_strings():
    p, t = _parse_tags(["random", "env=prod"])
    assert p is None
    assert t is None


def test_parse_tags_handles_json_string_payload():
    # LiteLLM sometimes serializes request_tags as a JSON-encoded list.
    p, t = _parse_tags('["project:7", "task:2.1"]')
    assert p == "7"
    assert t == "2.1"


def test_parse_tags_only_first_match_wins():
    p, t = _parse_tags(["project:1", "project:2", "task:1.1", "task:2.2"])
    assert p == "1"
    assert t == "1.1"


def test_parse_tags_rejects_four_part_task():
    # Task must be at most 3 dotted components.
    p, t = _parse_tags(["project:1", "task:1.2.3.4"])
    assert p == "1"
    assert t is None


# ---------------------------------------------------------------------------
# Project / task aggregation
# ---------------------------------------------------------------------------


def test_aggregate_groups_by_project_and_drills_down_into_tasks():
    rows = [
        _row(spend=1.0, total_tokens=100, request_tags=["project:1042", "task:3.1.2"]),
        _row(spend=2.0, total_tokens=200, request_tags=["project:1042", "task:3.1.2"]),
        _row(spend=4.0, total_tokens=400, request_tags=["project:1042", "task:3.2"]),
        _row(spend=8.0, total_tokens=800, request_tags=["project:2001", "task:1.1"]),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    projects = {p["project"]: p for p in out["projects"]}

    assert "1042" in projects
    assert "2001" in projects
    assert projects["1042"]["spend"] == pytest.approx(7.0)
    assert projects["1042"]["requests"] == 3
    assert projects["2001"]["spend"] == pytest.approx(8.0)

    tasks_1042 = {t["task"]: t for t in projects["1042"]["tasks"]}
    assert tasks_1042["3.1.2"]["spend"] == pytest.approx(3.0)
    assert tasks_1042["3.1.2"]["requests"] == 2
    assert tasks_1042["3.2"]["spend"] == pytest.approx(4.0)


def test_aggregate_orphan_task_falls_into_unattributed():
    # A task tag without a project tag is malformed and must NOT create
    # a phantom project bucket. It rolls into Unattributed instead so
    # the user sees their tagging gap.
    rows = [
        _row(spend=5.0, request_tags=["task:5.1"]),
        _row(spend=3.0, request_tags=["project:1042", "task:3.1"]),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    project_ids = [p["project"] for p in out["projects"]]
    assert project_ids == ["1042"]
    assert out["unattributed"]["spend"] == pytest.approx(5.0)
    assert out["unattributed"]["requests"] == 1


def test_aggregate_no_tags_at_all_is_unattributed():
    rows = [_row(spend=2.0, request_tags=[])]
    out = aggregate(user_info=None, spend_logs=rows)
    assert out["projects"] == []
    assert out["unattributed"]["spend"] == pytest.approx(2.0)


def test_aggregate_project_without_task_lists_placeholder_task():
    rows = [_row(spend=1.0, request_tags=["project:99"])]
    out = aggregate(user_info=None, spend_logs=rows)
    assert len(out["projects"]) == 1
    tasks = out["projects"][0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["task"] == UNATTRIBUTED


# ---------------------------------------------------------------------------
# Per-model and per-key breakdowns
# ---------------------------------------------------------------------------


def test_aggregate_breaks_down_by_model_with_cost_per_token():
    rows = [
        _row(model="gpt-4o", spend=0.10, total_tokens=1000),
        _row(model="gpt-4o", spend=0.20, total_tokens=2000),
        _row(model="claude-3-5-sonnet", spend=0.15, total_tokens=500),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    by_model = {m["key"]: m for m in out["models"]}
    assert by_model["gpt-4o"]["spend"] == pytest.approx(0.30)
    assert by_model["gpt-4o"]["total_tokens"] == 3000
    assert by_model["gpt-4o"]["cost_per_token"] == pytest.approx(0.0001)
    assert by_model["claude-3-5-sonnet"]["cost_per_token"] == pytest.approx(0.0003)
    # Sorted by spend descending.
    assert out["models"][0]["key"] == "gpt-4o"


def test_aggregate_keys_breakdown_uses_alias_when_available():
    rows = [
        _row(api_key="sk-realkey1234", spend=0.5, total_tokens=100),
        _row(api_key="sk-realkey1234", spend=1.5, total_tokens=200),
    ]
    keys_list = [{"token": "sk-realkey1234", "key_alias": "alice-prod"}]
    out = aggregate(user_info=None, spend_logs=rows, keys_list=keys_list)
    assert len(out["keys"]) == 1
    entry = out["keys"][0]
    assert entry["alias"] == "alice-prod"
    assert entry["spend"] == pytest.approx(2.0)
    assert entry["requests"] == 2


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def test_build_budget_derives_remaining_and_pct():
    info = {
        "spend": 25.0,
        "max_budget": 100.0,
        "soft_budget": 80.0,
        "budget_duration": "30d",
        "budget_reset_at": "2026-06-01T00:00:00Z",
    }
    b = build_budget(info)
    assert b["remaining"] == pytest.approx(75.0)
    assert b["consumed_pct"] == pytest.approx(25.0)
    assert b["soft_threshold_hit"] is False


def test_build_budget_flags_soft_threshold():
    info = {"spend": 90.0, "max_budget": 100.0, "soft_budget": 80.0}
    b = build_budget(info)
    assert b["soft_threshold_hit"] is True


def test_build_budget_handles_missing_max_budget():
    b = build_budget({"spend": 10.0, "max_budget": None})
    assert b["remaining"] is None
    assert b["consumed_pct"] is None
    assert b["spend"] == pytest.approx(10.0)


def test_build_budget_handles_none_input():
    b = build_budget(None)
    assert b["spend"] == 0.0
    assert b["remaining"] is None


# ---------------------------------------------------------------------------
# Tokens per request percentiles
# ---------------------------------------------------------------------------


def test_percentile_basic():
    assert _percentile([], 50) == 0.0
    assert _percentile([100], 50) == 100.0
    assert _percentile([10, 20, 30, 40, 50], 50) == 30.0
    assert _percentile([10, 20, 30, 40, 50], 95) == 50.0


def test_aggregate_tokens_per_request_only_counts_successful_token_volumes():
    # Zero-token rows (errors with no tokens) shouldn't drag the average
    # toward zero — they're not useful samples for the avg/p50/p95.
    rows = [
        _row(total_tokens=100),
        _row(total_tokens=200),
        _row(total_tokens=300),
        _row(total_tokens=0, spend=0, metadata={"status": "failure"}),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    tpr = out["tokens_per_request"]
    assert tpr["avg"] == pytest.approx(200.0)
    assert tpr["p50"] == 200.0


# ---------------------------------------------------------------------------
# Time series gap-fill
# ---------------------------------------------------------------------------


def test_time_series_fills_in_missing_days():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    rows = [
        _row(spend=1.0, total_tokens=100, startTime=base.isoformat()),
        _row(spend=2.0, total_tokens=200,
             startTime=(base + timedelta(days=3)).isoformat()),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    series = out["time_series"]
    assert [p["date"] for p in series] == [
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
    ]
    assert series[0]["spend"] == pytest.approx(1.0)
    assert series[1]["spend"] == 0.0
    assert series[3]["spend"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Status code classification
# ---------------------------------------------------------------------------


def test_status_codes_explicit_code_wins():
    rows = [
        _row(metadata={"status_code": 200}),
        _row(metadata={"status_code": 200}),
        _row(metadata={"status_code": 429}),
        _row(metadata={"status_code": 500}),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    assert out["status_codes"]["200"] == 2
    assert out["status_codes"]["429"] == 1
    assert out["status_codes"]["500"] == 1
    assert out["lifetime"]["successful_requests"] == 2
    assert out["lifetime"]["failed_requests"] == 2


def test_status_codes_falls_back_to_status_string():
    rows = [
        _row(metadata={"status": "failure"}, spend=0, total_tokens=0),
        _row(metadata={"status": "success"}),
    ]
    out = aggregate(user_info=None, spend_logs=rows)
    assert out["status_codes"]["error"] == 1
    assert out["status_codes"]["200"] == 1


# ---------------------------------------------------------------------------
# Period filtering
# ---------------------------------------------------------------------------


def test_current_period_only_includes_recent_rows():
    now = datetime.now(timezone.utc)
    rows = [
        _row(spend=1.0, startTime=(now - timedelta(days=1)).isoformat()),
        _row(spend=2.0, startTime=(now - timedelta(days=10)).isoformat()),
        _row(spend=4.0, startTime=(now - timedelta(days=60)).isoformat()),
    ]
    out = aggregate(user_info=None, spend_logs=rows, period_days=30)
    assert out["lifetime"]["spend"] == pytest.approx(7.0)
    assert out["current_period"]["spend"] == pytest.approx(3.0)
