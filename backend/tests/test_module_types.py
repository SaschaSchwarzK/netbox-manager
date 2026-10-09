from types import SimpleNamespace

import pytest
import requests

from app.module_type_schema import ModuleType
from app.routers import module_types as router
from app.services import diff, github_repo, module_types


BASE = {"manufacturer": "Acme", "model": "Line Card 1"}


def test_real_library_shape_round_trips_without_slug():
    payload = {
        **BASE,
        "part_number": "LC-1",
        "front-ports": [{"name": "F1", "type": "lc", "positions": 2}],
        "rear-ports": [{"name": "R1", "type": "lc", "positions": 2}],
        "port-mappings": [{"front_port": "F1", "rear_port": "R1"}],
        "module-bays": [{"name": "Submodule 1", "position": "A"}],
    }
    result = ModuleType(**payload).to_yaml_dict()
    assert result["manufacturer"] == "Acme"
    assert result["front-ports"][0]["name"] == "F1"
    assert "slug" not in result


def test_module_images_use_flat_folder_and_allow_front_and_rear(monkeypatch):
    entries = [
        SimpleNamespace(type="file", name="Line Card 1.front.png", path="module-images/Acme/Line Card 1.front.png"),
        SimpleNamespace(type="file", name="Line Card 1.rear.webp", path="module-images/Acme/Line Card 1.rear.webp"),
    ]
    monkeypatch.setattr(github_repo, "_repo", lambda *args: SimpleNamespace(get_contents=lambda *args, **kwargs: entries))
    monkeypatch.setattr(github_repo, "get_binary_file", lambda *args: github_repo.BinaryRepoFile(args[-1], b"image", "image/png"))
    images = github_repo.get_module_images("pat", "org/repo", "main", "module-types/Acme/Line Card 1.yaml")
    assert set(images) == {"front", "rear"}
    assert github_repo.module_image_destination(
        "module-types/Acme/Line Card 1.yaml", "front", "source.webp"
    ) == "module-images/Acme/Line Card 1.front.webp"


def test_module_images_reject_duplicate_side(monkeypatch):
    entries = [
        SimpleNamespace(type="file", name=f"card.front.{ext}", path=f"module-images/Acme/card.front.{ext}")
        for ext in ("png", "jpg")
    ]
    monkeypatch.setattr(github_repo, "_repo", lambda *args: SimpleNamespace(get_contents=lambda *args, **kwargs: entries))
    with pytest.raises(ValueError, match="Multiple front images"):
        github_repo.get_module_images("pat", "org/repo", "main", "module-types/Acme/card.yaml")


def test_generic_attachment_upload_uses_ui_backing_api(monkeypatch):
    calls = {}
    monkeypatch.setattr(module_types, "list_image_attachments", lambda *args: [])
    monkeypatch.setattr(module_types.requests, "post", lambda url, **kwargs: calls.update(url=url, **kwargs) or SimpleNamespace(raise_for_status=lambda: None))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 12
    image = github_repo.BinaryRepoFile("module-images/Acme/card.front.png", png, "image/png")
    module_types.upload_image_attachment("https://netbox", "token", True, 7, "front", image)
    assert calls["url"] == "https://netbox/api/extras/image-attachments/"
    assert calls["data"] == {"object_type": "dcim.moduletype", "object_id": "7", "name": "Front"}
    assert calls["files"]["image"][1] == png


def test_netbox_module_attachment_download_preserves_binary(monkeypatch):
    response = SimpleNamespace(
        content=b"webp", headers={"Content-Type": "image/webp; charset=binary"},
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(module_types.requests, "get", lambda *args, **kwargs: response)
    image = module_types.get_attachment_file(
        "https://netbox", "token", True, {"image": "/media/module-images/card.front.webp"}
    )
    assert image.path == "card.front.webp"
    assert image.content == b"webp"
    assert image.content_type == "image/webp"


def test_image_failure_is_non_fatal_to_module_push(monkeypatch):
    manufacturer = SimpleNamespace(id=2)
    created = SimpleNamespace(id=7)
    dcim = SimpleNamespace(
        manufacturers=SimpleNamespace(get=lambda **kwargs: manufacturer),
        module_types=SimpleNamespace(get=lambda **kwargs: None, create=lambda payload: created),
    )
    from app.module_type_schema import MODULE_COMPONENT_ENDPOINTS
    for endpoint_name in MODULE_COMPONENT_ENDPOINTS.values():
        setattr(dcim, endpoint_name.replace("-", "_"), SimpleNamespace(get=lambda **kwargs: None, create=lambda payload: None))
    monkeypatch.setattr(module_types, "get_client", lambda *args: SimpleNamespace(dcim=dcim))
    monkeypatch.setattr(module_types, "upload_image_attachment", lambda *args: (_ for _ in ()).throw(requests.RequestException("rejected")))
    image = github_repo.BinaryRepoFile("module-images/Acme/card.front.png", b"png", "image/png")
    result = module_types.push("https://netbox", "token", True, BASE, images={"front": image})
    assert result["status"] == "success"
    assert "image warning" in result["detail"]


def test_module_diff_reports_component_and_image_drift():
    existing = ModuleType(**BASE).to_internal_dict()
    result = diff.diff_module_type(
        {**BASE, "interfaces": [{"name": "eth0", "type": "1000base-t"}]},
        existing,
        [{"side": "front", "status": "target_missing"}],
    )
    assert result["status"] == "drift"
    assert result["component_changes"]["interfaces"]["added"] == ["eth0"]
    assert result["image_changes"][0]["side"] == "front"


def test_router_exposes_full_module_type_workflow():
    paths = {(route.path, method) for route in router.router.routes for method in route.methods}
    expected = {
        ("/api/repos/{target_id}/module-types", "GET"),
        ("/api/repos/{target_id}/module-types", "POST"),
        ("/api/repos/{target_id}/module-types/file", "GET"),
        ("/api/repos/{target_id}/module-types/file", "PUT"),
        ("/api/repos/{target_id}/module-types/file", "DELETE"),
        ("/api/repos/{target_id}/module-types/import", "POST"),
        ("/api/repos/{target_id}/module-types/file/coverage", "GET"),
        ("/api/repos/{target_id}/module-types/bulk-import", "POST"),
        ("/api/repos/{target_id}/module-types/file/diff-with-netbox", "POST"),
        ("/api/repos/{target_id}/module-types/file/push-to-netbox", "POST"),
        ("/api/repos/{target_id}/module-types/import-from-netbox/scan", "POST"),
        ("/api/repos/{target_id}/module-types/import-from-netbox/preview", "POST"),
        ("/api/repos/{target_id}/module-types/import-from-netbox", "POST"),
        ("/api/repos/{target_id}/module-types/bulk-import/image-preview", "POST"),
        ("/api/repos/{target_id}/module-types/import-from-netbox/image-preview", "POST"),
        ("/api/repos/{target_id}/module-types/file/image-preview", "GET"),
        ("/api/repos/{target_id}/module-types/file/image", "PUT"),
        ("/api/repos/{target_id}/module-types/file/image", "DELETE"),
    }
    assert expected <= paths
