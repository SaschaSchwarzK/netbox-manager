from app import auth


def setup_function():
    auth._local_login_failures.clear()


def test_different_ips_and_usernames_have_independent_buckets(monkeypatch):
    monkeypatch.setattr(auth.time, "monotonic", lambda: 1000.0)
    for _ in range(auth._LOCAL_LOGIN_MAX_FAILURES):
        auth.record_local_login_failure("192.0.2.1", "admin")
    assert auth.local_login_retry_after("192.0.2.1", "admin") is not None
    assert auth.local_login_retry_after("192.0.2.2", "admin") is None
    assert auth.local_login_retry_after("192.0.2.1", "other") is None


def test_lockout_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    for _ in range(auth._LOCAL_LOGIN_MAX_FAILURES):
        auth.record_local_login_failure("192.0.2.1", "admin")
    now[0] += auth._LOCAL_LOGIN_WINDOW_SECONDS + 1
    assert auth.local_login_retry_after("192.0.2.1", "admin") is None


def test_failure_bucket_count_is_bounded(monkeypatch):
    monkeypatch.setattr(auth, "_LOCAL_LOGIN_MAX_BUCKETS", 5)
    monkeypatch.setattr(auth.time, "monotonic", lambda: 1000.0)
    for number in range(20):
        auth.record_local_login_failure(f"192.0.2.{number}", "admin")
    assert len(auth._local_login_failures) <= 5
