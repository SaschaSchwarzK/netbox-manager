import errno
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from app.services import github_repo


@pytest.fixture(autouse=True)
def branch_head(monkeypatch):
    repo = SimpleNamespace(get_branch=lambda branch: SimpleNamespace(commit=SimpleNamespace(sha=f"sha-{branch}")))
    monkeypatch.setattr(github_repo, "_repo", lambda *args: repo)
    monkeypatch.setattr(github_repo, "_effective_archive_max_bytes", None)
    monkeypatch.setattr(github_repo, "_archive_limit_warning_emitted", False)
    monkeypatch.setattr(github_repo, "_archive_scan_semaphore", threading.BoundedSemaphore(1))


class FakeResponse:
    def __init__(self, content, status=200, headers=None):
        self.content, self.status_code, self.headers = content, status, headers or {}
        self.closed = False
    def close(self): self.closed = True
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            response = requests.Response(); response.status_code = self.status_code
            raise requests.HTTPError(response=response)
    def iter_content(self, chunk_size): yield self.content


def make_zip():
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("root/device-types/Acme/router.yaml", "manufacturer: Acme\nmodel: 9300\nslug: 9300\npart_number: 12345\n")
        archive.writestr("root/device-types/Acme/readme.txt", "ignored")
        archive.writestr("root/device-types/Acme/huge.yaml", b"x" * (github_repo._MAX_YAML_BYTES + 1))
    return data.getvalue()


def test_archive_scan_coerces_numeric_metadata_and_skips_large_files(monkeypatch):
    github_repo._metadata_cache.clear()
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: FakeResponse(make_zip()))
    rows = github_repo.scan_device_type_metadata("pat", "org/repo", "main", "device-types")
    assert rows == [{"path": "device-types/Acme/router.yaml", "manufacturer": "Acme",
                     "model": "9300", "slug": "9300", "part_number": "12345"}]


def test_archive_scan_uses_spooled_temporary_file(monkeypatch):
    used = []
    original = github_repo.tempfile.SpooledTemporaryFile
    monkeypatch.setattr(github_repo.tempfile, "SpooledTemporaryFile",
                        lambda *args, **kwargs: used.append(kwargs["max_size"]) or original(*args, **kwargs))
    github_repo._metadata_cache.clear()
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: FakeResponse(make_zip()))
    github_repo.scan_device_type_metadata("spooled", "org/repo", "main", "device-types")
    assert used == [github_repo._ARCHIVE_SPOOL_BYTES]


def test_archive_size_limit_is_enforced_while_streaming(monkeypatch):
    class ChunkedResponse(FakeResponse):
        chunks_read = 0

        def iter_content(self, chunk_size):
            for chunk in (b"1234", b"5678", b"not-read"):
                self.chunks_read += 1
                yield chunk

    github_repo._metadata_cache.clear()
    response = ChunkedResponse(b"")
    monkeypatch.setattr(github_repo, "initialize_archive_limit", lambda: 6)
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: response)
    with pytest.raises(ValueError, match="NBM_ARCHIVE_MAX_MIB"):
        github_repo.scan_device_type_metadata("limited", "org/repo", "main", "device-types")
    assert response.chunks_read == 2
    assert response.closed is True


def test_content_length_over_limit_rejects_before_spool_write(monkeypatch):
    writes = []
    monkeypatch.setattr(github_repo, "initialize_archive_limit", lambda: 6)
    response = FakeResponse(b"not-read", headers={"Content-Length": "7"})
    monkeypatch.setattr(
        github_repo.requests, "get",
        lambda *a, **k: response,
    )
    monkeypatch.setattr(
        github_repo.tempfile, "SpooledTemporaryFile",
        lambda **kwargs: writes.append(1) or (_ for _ in ()).throw(AssertionError("spool opened")),
    )

    with pytest.raises(ValueError, match="NBM_ARCHIVE_MAX_MIB"):
        github_repo.scan_device_type_metadata("declared-large", "org/repo", "main", "device-types")
    assert writes == []
    assert response.closed is True


def test_archive_scans_never_overlap_spool_section(monkeypatch):
    active = 0
    maximum = 0
    lock = threading.Lock()
    original = github_repo.tempfile.SpooledTemporaryFile

    class InstrumentedSpool:
        def __init__(self): self.file = original(max_size=github_repo._ARCHIVE_SPOOL_BYTES)
        def __enter__(self):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            self.file.__enter__()
            return self.file
        def __exit__(self, *args):
            nonlocal active
            try:
                return self.file.__exit__(*args)
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(github_repo.tempfile, "SpooledTemporaryFile", lambda **kwargs: InstrumentedSpool())
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: FakeResponse(make_zip()))
    errors = []

    def scan(index):
        try:
            github_repo.scan_device_type_metadata(f"parallel-{index}", "org/repo", "main", "device-types")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=scan, args=(index,)) for index in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert errors == []
    assert maximum == 1


def test_waiting_scan_reuses_cache_instead_of_redownloading(monkeypatch):
    github_repo._metadata_cache.clear()
    calls = []

    def download(*args, **kwargs):
        calls.append(1)
        time.sleep(0.02)
        return FakeResponse(make_zip())

    monkeypatch.setattr(github_repo.requests, "get", download)
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(github_repo.scan_device_type_metadata(
            "same-pat", "org/repo", "main", "device-types",
        )),
    ) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(results) == 2
    assert calls == [1]


def test_archive_lock_timeout_and_stream_failure_release_semaphore(monkeypatch):
    monkeypatch.setattr(github_repo.settings, "archive_lock_timeout_seconds", 0.01)
    assert github_repo._archive_scan_semaphore.acquire()
    try:
        with pytest.raises(github_repo.ArchiveBusyError, match="Another GitHub archive scan"):
            github_repo.scan_device_type_metadata("busy", "org/repo", "main", "device-types")
    finally:
        github_repo._archive_scan_semaphore.release()

    class BrokenResponse(FakeResponse):
        def iter_content(self, chunk_size):
            raise OSError("stream broke")

    response = BrokenResponse(b"")
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: response)
    with pytest.raises(OSError, match="stream broke"):
        github_repo.scan_device_type_metadata("broken", "org/repo", "main", "device-types")
    assert github_repo._archive_scan_semaphore.acquire(blocking=False)
    github_repo._archive_scan_semaphore.release()
    assert response.closed is True


def test_archive_spool_write_error_is_handled(monkeypatch):
    class FullSpool:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def write(self, chunk): raise OSError(errno.ENOSPC, "No space left on device")

    github_repo._metadata_cache.clear()
    monkeypatch.setattr(github_repo, "initialize_archive_limit", lambda: 48 * 1024 * 1024)
    monkeypatch.setattr(github_repo.tempfile, "SpooledTemporaryFile", lambda **kwargs: FullSpool())
    response = FakeResponse(make_zip())
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: response)
    with pytest.raises(ValueError, match="NBM_ARCHIVE_MAX_MIB"):
        github_repo.scan_device_type_metadata("full", "org/repo", "main", "device-types")
    assert response.closed is True


def test_low_temp_space_lowers_effective_archive_limit(monkeypatch):
    warnings = []
    monkeypatch.setattr(github_repo.settings, "archive_max_mib", 48)
    monkeypatch.setattr(
        github_repo.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=64 * 1024 * 1024, used=54 * 1024 * 1024, free=10 * 1024 * 1024),
    )

    monkeypatch.setattr(
        github_repo.logger, "warning",
        lambda message, value: warnings.append(message % value),
    )

    assert github_repo.initialize_archive_limit() == 2 * 1024 * 1024
    assert "lowering the effective NBM_ARCHIVE_MAX_MIB limit to 2.0 MiB" in warnings[0]


def test_archive_below_default_cap_succeeds(monkeypatch):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("root/padding.bin", b"x" * (40 * 1024 * 1024))
        archive.writestr(
            "root/device-types/Acme/router.yaml",
            "manufacturer: Acme\nmodel: Router\nslug: router\n",
        )
    github_repo._metadata_cache.clear()
    monkeypatch.setattr(github_repo, "initialize_archive_limit", lambda: 48 * 1024 * 1024)
    response = FakeResponse(data.getvalue())
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: response)

    assert github_repo.scan_device_type_metadata("forty", "org/repo", "main", "device-types")[0]["slug"] == "router"
    assert response.closed is True


def test_archive_busy_maps_to_retryable_service_unavailable():
    from app.routers._type_common import github_error_to_http

    error = github_error_to_http(github_repo.ArchiveBusyError("another scan"))
    assert error.status_code == 503
    assert error.headers == {"Retry-After": "5"}


def test_archive_too_large_status_mapping_is_unchanged():
    from app.routers._type_common import github_error_to_http

    error = github_error_to_http(ValueError("too large"))
    assert error.status_code == 500


def test_archive_cache_is_pat_scoped_and_bounded(monkeypatch):
    github_repo._metadata_cache.clear()
    calls = []
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: calls.append(1) or FakeResponse(make_zip()))
    for index in range(github_repo._MAX_METADATA_CACHE_ENTRIES + 2):
        github_repo.scan_device_type_metadata(f"pat-{index}", "org/repo", "main", "device-types")
    assert len(calls) == github_repo._MAX_METADATA_CACHE_ENTRIES + 2
    assert len(github_repo._metadata_cache) == github_repo._MAX_METADATA_CACHE_ENTRIES


def test_archive_cache_tracks_branch_head_sha(monkeypatch):
    github_repo._metadata_cache.clear()
    state = {"sha": "one"}
    repo = SimpleNamespace(get_branch=lambda branch: SimpleNamespace(
        commit=SimpleNamespace(sha=state["sha"])
    ))
    monkeypatch.setattr(github_repo, "_repo", lambda *args: repo)
    calls = []
    monkeypatch.setattr(github_repo.requests, "get", lambda url, **kwargs:
                        calls.append(url) or FakeResponse(make_zip()))

    github_repo.scan_device_type_metadata("pat", "org/repo", "main", "device-types")
    github_repo.scan_device_type_metadata("pat", "org/repo", "main", "device-types")
    state["sha"] = "two"
    github_repo.scan_device_type_metadata("pat", "org/repo", "main", "device-types")

    assert calls == [
        "https://api.github.com/repos/org/repo/zipball/one",
        "https://api.github.com/repos/org/repo/zipball/two",
    ]


def test_repository_write_invalidation_is_repo_scoped():
    github_repo._metadata_cache.clear()
    github_repo._metadata_cache[("org/repo", "sha", "device-types", "pat")] = (0, [])
    github_repo._metadata_cache[("other/repo", "sha", "device-types", "pat")] = (0, [])

    github_repo._invalidate_metadata("org/repo")

    assert list(github_repo._metadata_cache) == [("other/repo", "sha", "device-types", "pat")]


def test_source_coordinates_reject_traversal():
    import pytest
    for repo, branch in (("../repo", "main"), ("org/repo/extra", "main"), ("org/repo", "../main")):
        with pytest.raises(ValueError):
            github_repo.validate_source_coordinates(repo, branch)


def test_tree_scan_discovers_paths_without_downloading_archive(monkeypatch):
    monkeypatch.setattr(github_repo, "list_device_types", lambda *args: [
        github_repo.RepoFile("module-types/Acme/card.yaml", "sha1"),
        github_repo.RepoFile("module-types/Acme/fan.yml", "sha2"),
    ])
    monkeypatch.setattr(github_repo.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("archive download must not be used")
    ))
    assert github_repo.scan_repository_paths("pat", "org/repo", "main", "module-types") == [
        {"path": "module-types/Acme/card.yaml", "manufacturer": "Acme", "model": None,
         "slug": "card", "part_number": None},
        {"path": "module-types/Acme/fan.yml", "manufacturer": "Acme", "model": None,
         "slug": "fan", "part_number": None},
    ]
