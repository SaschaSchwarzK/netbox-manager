"""
Thin wrapper around pynetbox adding what a migration needs that a one-off
diff/push call (app.services.netbox_client) doesn't: proactive throttling,
retry with backoff on 429/5xx/timeouts, request accounting, and a hard
read-only guard for the source side.

Kept independent of FastAPI/SQLAlchemy so it's trivially unit-testable with
`responses`, matching the style of app.services.tenant_permission_backends.
"""
from __future__ import annotations

import random
import time
from functools import partial
from itertools import product
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import pynetbox
import requests

from app.services.netbox_client import get_client


FILTER_CHUNK_SIZE = 100


class ReadOnlyViolation(RuntimeError):
    """Raised if migration code attempts a write against a client marked read-only (the source)."""


class MigrationApiError(RuntimeError):
    """A request failed after exhausting retries."""


def _response_of(exc: Exception) -> requests.Response | None:
    """
    Extracts the underlying requests.Response from either exception type
    call() may see. Note a Response's __bool__ reflects .ok (False for any
    4xx/5xx), so callers must check `is not None`, never plain truthiness.
    """
    if isinstance(exc, pynetbox.RequestError):
        return getattr(exc, "req", None)
    if isinstance(exc, requests.HTTPError):
        return exc.response
    return None


def _retry_after_seconds(response: requests.Response | None) -> float | None:
    headers = getattr(response, "headers", None) if response is not None else None
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class RateLimitStats:
    requests_made: int = 0
    retries: int = 0
    throttle_sleeps: int = 0
    total_retry_sleep_seconds: float = 0.0
    total_throttle_sleep_seconds: float = 0.0


@dataclass
class RateLimitedClient:
    """
    Wraps a pynetbox.api instance. Every read/write goes through `call()`,
    which proactively throttles to `max_requests_per_second` and retries
    transient failures with exponential backoff + jitter.

    Not a full pynetbox proxy — migration code calls through `nb` for
    building queries (e.g. `client.nb.dcim.devices.filter(...)`) but wraps
    the actual iteration/creation calls in `client.call(...)` so every
    request — including the ones pynetbox issues lazily while iterating a
    Request generator — is retried uniformly. See `paginated()` below for
    the common "iterate a filter() result" case.
    """
    base_url: str
    nb: pynetbox.api
    read_only: bool = False
    max_requests_per_second: float = 4.0
    max_retries: int = 5
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    stats: RateLimitStats = field(default_factory=RateLimitStats)
    _last_request_at: float | None = field(default=None, init=False, repr=False)
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    _now: Callable[[], float] = field(default=time.monotonic, repr=False)
    _options_cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _options_error_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _options_disabled_reason: str | None = field(default=None, init=False, repr=False)

    def _throttle(self) -> None:
        if self.max_requests_per_second <= 0:
            return
        if self._last_request_at is not None:
            min_interval = 1.0 / self.max_requests_per_second
            remaining = min_interval - (self._now() - self._last_request_at)
            if remaining > 0:
                self.stats.throttle_sleeps += 1
                self.stats.total_throttle_sleep_seconds += remaining
                self._sleep(remaining)
        self._last_request_at = self._now()

    def call(self, fn: Callable[[], Any]) -> Any:
        """
        Runs `fn` (a zero-arg callable performing exactly one API round trip,
        e.g. `lambda: nb.dcim.sites.create(payload)`) with throttling and
        retry. Retries on HTTP 429 (honoring Retry-After) and on 5xx/timeout/
        connection errors; never retries on other 4xx (those are genuine
        validation errors that won't succeed on retry).
        """
        attempt = 0
        while True:
            self._throttle()
            try:
                result = fn()
                self.stats.requests_made += 1
                return result
            except (pynetbox.RequestError, requests.HTTPError) as exc:
                self.stats.requests_made += 1
                response = _response_of(exc)
                status = getattr(response, "status_code", None) if response is not None else None
                if status == 429:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise MigrationApiError(f"Rate limited after {self.max_retries} retries: {exc}") from exc
                    self._backoff_sleep(attempt, override=_retry_after_seconds(response))
                    continue
                if status is not None and 500 <= status < 600:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise MigrationApiError(f"Server error after {self.max_retries} retries: {exc}") from exc
                    self._backoff_sleep(attempt)
                    continue
                raise  # genuine 4xx validation error — do not retry
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.stats.requests_made += 1
                attempt += 1
                if attempt > self.max_retries:
                    raise MigrationApiError(f"Connection error after {self.max_retries} retries: {exc}") from exc
                self._backoff_sleep(attempt)
                continue

    def _backoff_sleep(self, attempt: int, override: float | None = None) -> None:
        if override is not None:
            delay = override
        else:
            delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)  # jitter
        self.stats.retries += 1
        self.stats.total_retry_sleep_seconds += delay
        self._sleep(delay)

    def _fetch_page(self, url: str, params: dict[str, Any] | None, headers: dict[str, str]) -> requests.Response:
        response = self.nb.http_session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        return response

    def paginated(self, endpoint, **filters) -> Iterator[dict[str, Any]]:
        """
        Pages through an endpoint's list view with retry applied per HTTP
        page fetch (not per record).

        Deliberately does NOT use pynetbox's own `endpoint.filter()`
        generator: that generator fetches subsequent pages lazily as you
        iterate it, and if an HTTP error occurs mid-fetch the exception
        propagates out of the generator's frame — which permanently
        exhausts it. Python generators cannot be resumed after that; the
        next `next()` call raises StopIteration rather than retrying. So
        long paginated reads are done here as plain HTTP against the same
        pynetbox-managed session/auth/TLS settings, one page at a time,
        with each page fetch retried independently via `call()`.

        Yields raw dicts (NetBox's `results` array entries) rather than
        pynetbox Record objects, since matching/sanitization work on plain
        dicts throughout the migration engine.
        """
        headers = {"accept": "application/json", "authorization": f"Token {self.nb.token}"}
        list_filters = {
            key: value for key, value in filters.items()
            if isinstance(value, list) and len(value) > FILTER_CHUNK_SIZE
        }
        chunks = [
            [values[index:index + FILTER_CHUNK_SIZE] for index in range(0, len(values), FILTER_CHUNK_SIZE)]
            for values in list_filters.values()
        ]
        chunk_combinations = product(*chunks) if chunks else [()]
        seen_ids: set[int] = set()

        for chunk_values in chunk_combinations:
            chunk_filters = dict(filters)
            for key, values in zip(list_filters, chunk_values):
                chunk_filters[key] = values
            url: str | None = endpoint.url if endpoint.url.endswith("/") else f"{endpoint.url}/"
            params: dict[str, Any] | None = chunk_filters
            while url:
                request_url, request_params = url, params

                response = self.call(partial(self._fetch_page, request_url, request_params, headers))
                data = response.json()
                for result in data.get("results", []):
                    result_id = result.get("id") if isinstance(result, dict) else None
                    if isinstance(result_id, int) and result_id > 0:
                        if result_id in seen_ids:
                            continue
                        seen_ids.add(result_id)
                    yield result
                url = data.get("next")
                params = None  # `next` is already a fully-formed URL with its own query string

    def create(self, endpoint, payload: dict[str, Any]) -> Any:
        self._guard_write()
        return self.call(lambda: endpoint.create(payload))

    def create_many(self, endpoint, payloads: list[dict[str, Any]]) -> list[Any]:
        """Create one NetBox bulk payload and reject ambiguous response shapes."""
        self._guard_write()
        result = self.call(lambda: endpoint.create(payloads))
        if isinstance(result, (str, bytes, dict)):
            raise MigrationApiError("NetBox bulk create returned a non-list response")
        try:
            records = list(result)
        except TypeError as exc:
            raise MigrationApiError("NetBox bulk create returned a non-list response") from exc
        if len(records) != len(payloads):
            raise MigrationApiError(
                f"NetBox bulk create returned {len(records)} records for {len(payloads)} payloads"
            )
        return records

    def update(self, record: Any, payload: dict[str, Any]) -> Any:
        self._guard_write()
        return self.call(lambda: record.update(payload))

    def update_by_id(self, endpoint, id_: int, payload: dict[str, Any]) -> Any:
        """Like `update`, but for callers that only have a target id (e.g. the executor's patch phase), not an already-fetched Record."""
        self._guard_write()
        record = self.call(lambda: endpoint.get(id_))
        if record is None:
            raise MigrationApiError(f"Cannot update {endpoint}: id={id_} no longer exists on the target")
        return self.call(lambda: record.update(payload))

    def delete_by_id(self, endpoint, id_: int) -> bool:
        """Delete an object by id; an already-absent object is an idempotent success."""
        self._guard_write()
        record = self.call(lambda: endpoint.get(id_))
        if record is None:
            return False
        self.call(lambda: record.delete())
        return True

    def get(self, endpoint, **filters) -> Any:
        return self.call(lambda: endpoint.get(**filters))

    def options(self, endpoint) -> dict[str, Any]:
        """Return cached endpoint metadata used for plan-time field warnings."""
        # pynetbox exposes endpoint URLs without the trailing slash. Avoid a
        # redirect (which can lose authentication through some proxies) and
        # remember failures too: OPTIONS validation is advisory, so a target
        # that forbids it must cost one request per endpoint, not one request
        # per migrated object.
        key = endpoint.url.rstrip("/") + "/"
        if self._options_disabled_reason is not None:
            raise MigrationApiError(self._options_disabled_reason)
        if key in self._options_error_cache:
            raise MigrationApiError(self._options_error_cache[key])
        if key not in self._options_cache:
            try:
                response = self.call(lambda: self.nb.http_session.options(key, timeout=30))
                response.raise_for_status()
                data = response.json()
                self._options_cache[key] = data if isinstance(data, dict) else {}
            except Exception as exc:
                message = str(exc)
                self._options_error_cache[key] = message
                response = _response_of(exc)
                if response is not None and response.status_code in (401, 403):
                    self._options_disabled_reason = message
                raise MigrationApiError(message) from exc
        return self._options_cache[key]

    def _guard_write(self) -> None:
        if self.read_only:
            raise ReadOnlyViolation(
                f"Refusing to write to {self.base_url}: this client is the migration source and must stay read-only."
            )


def build_client(
    base_url: str,
    token: str,
    verify_ssl: bool,
    *,
    read_only: bool,
    max_requests_per_second: float = 4.0,
) -> RateLimitedClient:
    nb = get_client(base_url, token, verify_ssl)
    return RateLimitedClient(
        base_url=base_url, nb=nb, read_only=read_only, max_requests_per_second=max_requests_per_second,
    )
