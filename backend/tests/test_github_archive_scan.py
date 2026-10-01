from io import BytesIO
from zipfile import ZipFile

from app.services import github_repo


class FakeResponse:
    def __init__(self, content, status=200): self.content, self.status_code = content, status
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


def test_archive_cache_is_pat_scoped_and_bounded(monkeypatch):
    github_repo._metadata_cache.clear()
    calls = []
    monkeypatch.setattr(github_repo.requests, "get", lambda *a, **k: calls.append(1) or FakeResponse(make_zip()))
    for index in range(github_repo._MAX_METADATA_CACHE_ENTRIES + 2):
        github_repo.scan_device_type_metadata(f"pat-{index}", "org/repo", "main", "device-types")
    assert len(calls) == github_repo._MAX_METADATA_CACHE_ENTRIES + 2
    assert len(github_repo._metadata_cache) == github_repo._MAX_METADATA_CACHE_ENTRIES


def test_source_coordinates_reject_traversal():
    import pytest
    for repo, branch in (("../repo", "main"), ("org/repo/extra", "main"), ("org/repo", "../main")):
        with pytest.raises(ValueError):
            github_repo.validate_source_coordinates(repo, branch)
