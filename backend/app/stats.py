from collections import Counter, defaultdict, deque
from threading import Lock

_lock = Lock()
_requests = defaultdict(lambda: deque(maxlen=1000))
_request_counts = Counter()
_outbound = Counter()
_last_drift_run = None


def record_request(route: str, duration_ms: float) -> None:
    with _lock:
        _requests[route].append(duration_ms)
        _request_counts[route] += 1


def record_outbound(name: str, count: int = 1) -> None:
    with _lock:
        _outbound[name] += count


def record_drift_run(summary: dict) -> None:
    global _last_drift_run
    with _lock:
        _last_drift_run = dict(summary)


def snapshot() -> dict:
    with _lock:
        routes = {}
        for route, samples in _requests.items():
            ordered = sorted(samples)
            percentile = lambda value: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * value))]
            routes[route] = {
                "count": _request_counts[route], "sample_count": len(ordered),
                "p50_ms": round(percentile(0.50), 2), "p95_ms": round(percentile(0.95), 2),
            }
        result = {"routes": routes, "outbound_calls": dict(_outbound)}
        if _last_drift_run is not None:
            result["last_drift_run"] = dict(_last_drift_run)
        return result
