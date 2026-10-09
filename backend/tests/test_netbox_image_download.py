from types import SimpleNamespace

import pytest

from app.services import netbox_client


def response(chunks, *, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, raise_for_status=lambda: None,
                           iter_content=lambda chunk_size: iter(chunks))


def mock_get(monkeypatch, callback):
    monkeypatch.setattr(netbox_client, "get_session", lambda *args: SimpleNamespace(get=callback))


def test_external_image_host_is_rejected_without_network_call(monkeypatch):
    mock_get(monkeypatch, lambda *a, **k: pytest.fail("network called"))
    with pytest.raises(ValueError, match="configured NetBox"):
        netbox_client.get_image_bytes("https://evil.example/image.png", "secret", True,
                                      "https://netbox.example")


def test_relative_image_url_resolves_on_instance(monkeypatch):
    captured = {}
    mock_get(monkeypatch,
             lambda url, **kwargs: captured.update(url=url, kwargs=kwargs) or response([b"png"]))
    assert netbox_client.get_image_bytes("/media/image.png", "token", True,
                                         "https://netbox.example") == b"png"
    assert captured["url"] == "https://netbox.example/media/image.png"
    assert captured["kwargs"]["allow_redirects"] is False


def test_oversize_stream_is_rejected(monkeypatch):
    monkeypatch.setattr(netbox_client, "_MAX_IMAGE_BYTES", 4)
    mock_get(monkeypatch, lambda *a, **k: response([b"123", b"45"]))
    with pytest.raises(ValueError, match="10 MiB"):
        netbox_client.get_image_bytes("/media/image.png", "token", True, "https://netbox.example")


def test_redirect_is_rejected(monkeypatch):
    mock_get(monkeypatch, lambda *a, **k: response([], status=302,
             headers={"Location": "https://evil.example/image.png"}))
    with pytest.raises(ValueError, match="redirect"):
        netbox_client.get_image_bytes("/media/image.png", "token", True, "https://netbox.example")
