from app import stats
from app.main import admin_stats
from app.rbac import AccessContext


def test_request_stats_are_bounded_and_report_percentiles():
    route = "GET /test-observability"
    for value in range(1200):
        stats.record_request(route, float(value))
    result = stats.snapshot()["routes"][route]
    assert result["count"] == 1200
    assert result["sample_count"] == 1000
    assert result["p95_ms"] >= result["p50_ms"]


def test_admin_stats_returns_outbound_totals():
    stats.record_outbound("netbox", 2)
    result = admin_stats(AccessContext(role="admin", app_admin=True))
    assert result["outbound_calls"]["netbox"] >= 2


def test_admin_stats_exposes_last_drift_run_summary():
    stats.record_drift_run({"pairs": 20, "degraded": True})
    result = admin_stats(AccessContext(role="admin", app_admin=True))
    assert result["last_drift_run"] == {"pairs": 20, "degraded": True}
