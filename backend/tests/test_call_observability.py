import httpx
import requests
import responses
from sqlalchemy import create_engine, text

from app.observability import (
    get_request_stats, install_db_instrumentation, reset_request_stats, start_request_stats,
)


@responses.activate
def test_requests_are_classified_without_recording_request_data():
    responses.add(responses.GET, "https://api.github.com/repos/o/r", json={})
    responses.add(responses.GET, "https://netbox.example/api/status/", json={})
    token = start_request_stats()
    try:
        requests.get("https://api.github.com/repos/o/r", headers={"Authorization": "secret"})
        requests.get("https://netbox.example/api/status/")
        captured = get_request_stats()
        assert captured.outbound["github"].count == 1
        assert captured.outbound["netbox:netbox.example"].count == 1
        assert "secret" not in repr(captured)
    finally:
        reset_request_stats(token)


def test_httpx_and_database_timing_feed_the_current_context():
    engine = create_engine("sqlite:///:memory:")
    install_db_instrumentation(engine)
    token = start_request_stats()
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            client.get("https://service.example/health")
        with engine.begin() as connection:
            connection.execute(text("SELECT 42"))
        captured = get_request_stats()
        assert captured.outbound["other"].count == 1
        assert captured.db_count == 1
        assert captured.slowest_db_statement == "SELECT ?"
    finally:
        reset_request_stats(token)
