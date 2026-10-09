import asyncio
import json
from types import SimpleNamespace

from app import main
from app.main import RequestMetricsMiddleware


def run_request(request_id: bytes):
    sent = []
    scope = {"type": "http", "method": "GET", "path": "/items/42",
             "headers": [(b"x-request-id", request_id)]}

    async def application(inner_scope, _receive, send):
        inner_scope["route"] = SimpleNamespace(path="/items/{item_id}")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(RequestMetricsMiddleware(application)(scope, receive, send))
    return sent


def test_timing_middleware_uses_route_template_and_safe_request_id(monkeypatch):
    records = []
    monkeypatch.setattr(main.logger, "log", lambda level, message: records.append((level, message)))
    sent = run_request(b"caller-123")
    headers = dict(sent[0]["headers"])
    assert headers[b"x-request-id"] == b"caller-123"
    payload = json.loads(records[0][1])
    assert payload["route"] == "/items/{item_id}"
    assert payload["status"] == 204
    assert payload["db_count"] == 0


def test_timing_middleware_replaces_unsafe_request_id():
    sent = run_request(b"unsafe id with spaces and far too much untrusted content" * 3)
    generated = dict(sent[0]["headers"])[b"x-request-id"]
    assert generated != b"unsafe id with spaces and far too much untrusted content" * 3
    assert len(generated) == 36
