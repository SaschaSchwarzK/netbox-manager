from types import SimpleNamespace

import pytest
import requests
from github import UnknownObjectException

from app.devicetype_schema import DeviceType
from app.devicetype_schema import COMPONENT_ENDPOINTS
from app.routers import device_types
from app.services import diff, github_repo, netbox_client


BASE = {"manufacturer": "Cisco", "model": "C9300-24P", "slug": "c9300-24p"}


def test_get_binary_file_returns_raw_bytes_and_content_type(monkeypatch):
    blob = SimpleNamespace(decoded_content=b"\x89PNG\r\n", sha="abc")
    repo = SimpleNamespace(get_contents=lambda path, ref: blob)
    monkeypatch.setattr(github_repo, "_repo", lambda *args: repo)

    result = github_repo.get_binary_file("pat", "owner/repo", "main", "images/example.png")

    assert result.content == b"\x89PNG\r\n"
    assert result.content_type == "image/png"


def test_get_binary_file_propagates_missing_file(monkeypatch):
    def missing(path, ref):
        raise UnknownObjectException(404, {"message": "Not Found"}, {})

    monkeypatch.setattr(github_repo, "_repo", lambda *args: SimpleNamespace(get_contents=missing))
    with pytest.raises(UnknownObjectException):
        github_repo.get_binary_file("pat", "owner/repo", "main", "missing.png")


def test_elevation_image_uses_flat_manufacturer_folder(monkeypatch):
    entry = SimpleNamespace(
        type="file", name="c9300-24p.front.png",
        path="elevation-images/Cisco/c9300-24p.front.png",
    )
    repo = SimpleNamespace(get_contents=lambda path, ref: [entry])
    monkeypatch.setattr(github_repo, "_repo", lambda *args: repo)
    monkeypatch.setattr(
        github_repo, "get_binary_file",
        lambda *args: github_repo.BinaryRepoFile(args[-1], b"png", "image/png"),
    )

    result = github_repo.get_elevation_image(
        "pat", "owner/repo", "main", "device-types/Cisco/c9300-24p.yml", "c9300-24p", "front"
    )

    assert result.path == "elevation-images/Cisco/c9300-24p.front.png"


def test_elevation_image_prefers_slug_name_and_falls_back_to_yaml_stem(monkeypatch):
    entries = [
        SimpleNamespace(type="file", name="N9K-C9396TX.front.png", path="elevation-images/Cisco/N9K-C9396TX.front.png"),
        SimpleNamespace(type="file", name="cisco-n9k-c9396tx.front.png", path="elevation-images/Cisco/cisco-n9k-c9396tx.front.png"),
    ]
    monkeypatch.setattr(github_repo, "_repo", lambda *args: SimpleNamespace(get_contents=lambda *args, **kwargs: entries))
    monkeypatch.setattr(github_repo, "get_binary_file", lambda *args: github_repo.BinaryRepoFile(args[-1], b"png", "image/png"))
    canonical = github_repo.get_elevation_image(
        "pat", "owner/repo", "master", "device-types/Cisco/N9K-C9396TX.yaml",
        "cisco-n9k-c9396tx", "front",
    )
    assert canonical.path.endswith("cisco-n9k-c9396tx.front.png")
    entries.pop()
    legacy = github_repo.get_elevation_image(
        "pat", "owner/repo", "master", "device-types/Cisco/N9K-C9396TX.yaml",
        "cisco-n9k-c9396tx", "front",
    )
    assert legacy.path.endswith("N9K-C9396TX.front.png")


def test_elevation_image_destination_preserves_extension():
    assert github_repo.elevation_image_destination(
        "device-types/Cisco/c9300-24p.yml", "c9300-24p", "rear", "source-image.webp"
    ) == "elevation-images/Cisco/c9300-24p.rear.webp"


def test_bulk_create_files_commits_binary_content_unchanged(monkeypatch):
    created = []

    def missing(path, ref):
        raise UnknownObjectException(404, {"message": "Not Found"}, {})

    repo = SimpleNamespace(
        get_branch=lambda branch: SimpleNamespace(commit=SimpleNamespace(sha="base-sha")),
        get_git_tree=lambda sha, recursive: SimpleNamespace(tree=[], truncated=False),
        create_git_ref=lambda **kwargs: None,
        get_contents=missing,
        create_file=lambda path, message, content, branch: created.append((path, content)),
    )
    monkeypatch.setattr(github_repo, "_repo", lambda *args: repo)

    result = github_repo.bulk_create_files(
        "pat", "owner/repo", "main", "import/test",
        [{"path": "elevation-images/Cisco/example.front.png", "content": b"\x89PNG"}],
        "Import",
    )

    assert result["created"] == ["elevation-images/Cisco/example.front.png"]
    assert created == [("elevation-images/Cisco/example.front.png", b"\x89PNG")]


def test_coverage_image_check_warns_when_declared_file_is_missing(monkeypatch):
    monkeypatch.setattr(github_repo, "get_elevation_image", lambda *args: None)
    target = SimpleNamespace(repo="owner/repo", branch="main")

    images, warnings = device_types._load_elevation_images(
        "pat", target, "device-types/Cisco/c9300-24p.yml", {**BASE, "front_image": True}
    )

    assert images == {}
    assert warnings == ["Front image is declared but missing from the repository"]


def test_push_succeeds_when_multipart_image_upload_fails(monkeypatch):
    manufacturer = SimpleNamespace(id=9)
    device_type = SimpleNamespace(id=42)
    nb = SimpleNamespace(dcim=SimpleNamespace(
        manufacturers=SimpleNamespace(get=lambda **kwargs: manufacturer),
        device_types=SimpleNamespace(get=lambda **kwargs: None, create=lambda payload: device_type),
    ))
    monkeypatch.setattr(netbox_client, "get_client", lambda *args: nb)
    monkeypatch.setattr(netbox_client, "get_session", lambda *args: SimpleNamespace(
        patch=lambda *args, **kwargs: (_ for _ in ()).throw(requests.RequestException("upload rejected"))
    ))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 12
    image = github_repo.BinaryRepoFile("elevation-images/Cisco/c9300-24p.front.png", png, "image/png")

    result = netbox_client.push_device_type(
        "https://netbox", "token", True, {**BASE, "front_image": True}, images={"front": image}
    )

    assert result["status"] == "success"
    assert "Could not upload front image" in result["detail"]


def test_image_upload_uses_separate_multipart_patch(monkeypatch):
    captured = {}
    response = SimpleNamespace(raise_for_status=lambda: None)

    def patch(url, **kwargs):
        captured.update(url=url, **kwargs)
        return response

    monkeypatch.setattr(netbox_client, "get_session", lambda *args: SimpleNamespace(patch=patch))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 12
    image = github_repo.BinaryRepoFile("elevation-images/Cisco/c9300-24p.front.png", png, "image/png")

    netbox_client.upload_device_type_image("https://netbox", "token", True, 42, "front", image)

    assert captured["url"] == "https://netbox/api/dcim/device-types/42/"
    assert captured["files"] == {"front_image": ("c9300-24p.front.png", png, "image/png")}
    assert "json" not in captured


def test_netbox_image_download_retains_bytes_filename_and_content_type(monkeypatch):
    response = SimpleNamespace(
        status_code=200, headers={"Content-Type": "image/webp; charset=binary"},
        raise_for_status=lambda: None,
        iter_content=lambda chunk_size: iter([b"webp"]),
    )
    monkeypatch.setattr(netbox_client, "get_session", lambda *args: SimpleNamespace(
        get=lambda *args, **kwargs: response
    ))

    result = netbox_client.get_image_file(
        "https://netbox/media/devicetype-images/example.rear.webp", "token", True, "https://netbox"
    )

    assert result.path == "example.rear.webp"
    assert result.content == b"webp"
    assert result.content_type == "image/webp"


def test_netbox_device_type_images_become_yaml_flags_plus_private_urls(monkeypatch):
    manufacturer = SimpleNamespace(id=9)
    device_type = SimpleNamespace(
        id=42, model="C9300-24P", slug="c9300-24p", part_number="", u_height=1,
        is_full_depth=True, subdevice_role=None, weight=None, weight_unit=None,
        comments="", custom_fields={}, front_image="/media/front.png", rear_image=None,
    )
    dcim = SimpleNamespace(
        manufacturers=SimpleNamespace(get=lambda **kwargs: manufacturer),
        device_types=SimpleNamespace(get=lambda **kwargs: device_type),
    )
    for endpoint_name in COMPONENT_ENDPOINTS.values():
        setattr(dcim, endpoint_name.replace("-", "_"), SimpleNamespace(filter=lambda **kwargs: []))
    monkeypatch.setattr(netbox_client, "get_client", lambda *args: SimpleNamespace(dcim=dcim))

    result = netbox_client.get_existing_device_type(
        "https://netbox", "token", True, "Cisco", "c9300-24p"
    )

    assert result["front_image"] is True
    assert result["rear_image"] is None
    assert result["_image_urls"] == {"front": "/media/front.png", "rear": None}
    assert DeviceType(**result).front_image is True


def test_diff_reports_image_drift_separately():
    existing = DeviceType(**BASE).to_internal_dict()
    existing["front_image"] = None
    result = diff.diff_payloads(
        {**BASE, "front_image": True}, existing,
        [{"side": "front", "status": "target_missing"}],
    )

    assert result["status"] == "drift"
    assert result["base_field_changes"] == []
    assert result["image_changes"] == [{"side": "front", "status": "target_missing"}]


def test_bulk_import_exposes_on_demand_image_preview_routes():
    paths = {(route.path, method) for route in device_types.router.routes for method in route.methods}
    assert ("/api/repos/{target_id}/device-types/bulk-import/image-preview", "POST") in paths
    assert ("/api/repos/{target_id}/device-types/import-from-netbox/image-preview", "POST") in paths
    assert ("/api/repos/{target_id}/device-types/file/image-preview", "GET") in paths
    assert ("/api/repos/{target_id}/device-types/file/image", "PUT") in paths
    assert ("/api/repos/{target_id}/device-types/file/image", "DELETE") in paths


def test_image_preview_request_models_resolve_at_runtime():
    from app import schemas
    github_request = schemas.BulkImportImagePreviewRequest(
        source_repo="netbox-community/devicetype-library", source_branch="master",
        path="device-types/Cisco/APIC-L3.yaml", side="front",
    )
    netbox_request = schemas.ImportFromNetboxImagePreviewRequest(
        instance_id="instance", manufacturer="Cisco", slug="cisco-apic-l3", side="rear",
    )
    assert github_request.side == "front"
    assert netbox_request.side == "rear"


def test_apic_l3_bulk_image_preview_returns_requested_image(monkeypatch):
    from app import schemas
    target = SimpleNamespace(id="target", repo="destination/repo", branch="main", pat_encrypted="encrypted")
    db = SimpleNamespace(get=lambda model, target_id: target)
    monkeypatch.setattr(device_types.crypto, "decrypt", lambda value: "pat")
    monkeypatch.setattr(github_repo, "get_file", lambda *args: {"payload": {
        "manufacturer": "Cisco", "model": "APIC-L3", "slug": "cisco-apic-l3",
        "front_image": True, "rear_image": True,
    }})
    monkeypatch.setattr(github_repo, "get_elevation_image", lambda *args: github_repo.BinaryRepoFile(
        "elevation-images/Cisco/cisco-apic-l3.front.png", b"png", "image/png"
    ))
    result = device_types.bulk_import_image_preview(
        "target", schemas.BulkImportImagePreviewRequest(
            source_repo="netbox-community/devicetype-library", source_branch="master",
            path="device-types/Cisco/APIC-L3.yaml", side="front",
        ), db=db, ctx=None,
    )
    assert result.filename == "cisco-apic-l3.front.png"
    assert result.content_base64 == "cG5n"


def test_bulk_preview_discovers_images_when_yaml_flags_are_absent(monkeypatch):
    from app import schemas
    target = SimpleNamespace(id="target", repo="destination/repo", branch="main", pat_encrypted="encrypted")
    db = SimpleNamespace(get=lambda model, target_id: target)
    monkeypatch.setattr(device_types.crypto, "decrypt", lambda value: "pat")
    monkeypatch.setattr(github_repo, "validate_source_coordinates", lambda *args: None)
    monkeypatch.setattr(github_repo, "get_file", lambda *args: {"payload": {
        "manufacturer": "Cisco", "model": "Nexus 9396TX", "slug": "cisco-n9k-c9396tx",
    }})
    monkeypatch.setattr(github_repo, "get_elevation_image", lambda *args: github_repo.BinaryRepoFile(
        f"elevation-images/Cisco/cisco-n9k-c9396tx.{args[-1]}.png", b"png", "image/png"
    ))
    preview = device_types.bulk_import_preview(
        "target", schemas.BulkImportPreviewRequest(
            source_repo="netbox-community/devicetype-library", source_branch="master",
            path="device-types/Cisco/N9K-C9396TX.yaml",
        ), db=db, ctx=None,
    )
    assert preview.image_status == {"front": "present", "rear": "present"}
