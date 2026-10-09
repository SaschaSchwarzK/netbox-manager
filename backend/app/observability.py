from __future__ import annotations

import contextvars
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from urllib.parse import urlparse

import httpx
import requests
from sqlalchemy import event

from app import stats


@dataclass
class CallMetric:
    count: int = 0
    seconds: float = 0.0


@dataclass
class CallStats:
    outbound: dict[str, CallMetric] = field(default_factory=lambda: defaultdict(CallMetric))
    db_count: int = 0
    db_seconds: float = 0.0
    slowest_db_seconds: float = 0.0
    slowest_db_statement: str | None = None


_current_stats: contextvars.ContextVar[CallStats | None] = contextvars.ContextVar("call_stats", default=None)
_install_lock = Lock()
_http_installed = False
_db_engines: set[int] = set()
_literal_re = re.compile(r"'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b")


def start_request_stats() -> contextvars.Token:
    return _current_stats.set(CallStats())


def get_request_stats() -> CallStats:
    current = _current_stats.get()
    return current if current is not None else CallStats()


def reset_request_stats(token: contextvars.Token) -> None:
    _current_stats.reset(token)


def classify_url(url: object) -> str:
    parsed = urlparse(str(url))
    host = (parsed.hostname or "unknown").lower()
    if host == "github.com" or host.endswith(".github.com") or host.endswith(".githubusercontent.com"):
        return "github"
    if parsed.path.startswith("/api/") or parsed.path == "/api":
        return f"netbox:{host}"
    return "other"


def _record_outbound(url: object, elapsed: float) -> None:
    name = classify_url(url)
    current = _current_stats.get()
    if current is not None:
        metric = current.outbound[name]
        metric.count += 1
        metric.seconds += elapsed
    stats.record_outbound(name)


def install_http_instrumentation() -> None:
    global _http_installed
    with _install_lock:
        if _http_installed:
            return
        original_requests_send = requests.Session.send
        original_httpx_send = httpx.Client.send
        original_httpx_async_send = httpx.AsyncClient.send

        def requests_send(session, request, **kwargs):
            started = time.perf_counter()
            try:
                return original_requests_send(session, request, **kwargs)
            finally:
                _record_outbound(request.url, time.perf_counter() - started)

        def httpx_send(client, request, **kwargs):
            started = time.perf_counter()
            try:
                return original_httpx_send(client, request, **kwargs)
            finally:
                _record_outbound(request.url, time.perf_counter() - started)

        async def httpx_async_send(client, request, **kwargs):
            started = time.perf_counter()
            try:
                return await original_httpx_async_send(client, request, **kwargs)
            finally:
                _record_outbound(request.url, time.perf_counter() - started)

        requests.Session.send = requests_send
        httpx.Client.send = httpx_send
        httpx.AsyncClient.send = httpx_async_send
        _http_installed = True


def _clean_statement(statement: str) -> str:
    return _literal_re.sub("?", " ".join(statement.split()))[:1000]


def install_db_instrumentation(engine) -> None:
    with _install_lock:
        identity = id(engine)
        if identity in _db_engines:
            return
        _db_engines.add(identity)

    @event.listens_for(engine, "before_cursor_execute")
    def before_cursor_execute(_conn, _cursor, _statement, _parameters, context, _executemany):
        context._nbm_observability_started = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def after_cursor_execute(_conn, _cursor, statement, _parameters, context, _executemany):
        current = _current_stats.get()
        if current is None:
            return
        elapsed = time.perf_counter() - context._nbm_observability_started
        current.db_count += 1
        current.db_seconds += elapsed
        if elapsed > current.slowest_db_seconds:
            current.slowest_db_seconds = elapsed
            current.slowest_db_statement = _clean_statement(statement)


install_http_instrumentation()
