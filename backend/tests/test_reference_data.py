import pytest

from app.services import reference_data


@pytest.mark.parametrize("kind", reference_data.REGISTRY)
def test_registry_models_are_strict(kind):
    with pytest.raises(Exception):
        reference_data.validate_file(kind, {"items": [{"name": "x", "unknown": True}]})


@pytest.mark.parametrize("kind", reference_data.REGISTRY)
def test_registry_rejects_synced_fields(kind):
    with pytest.raises(ValueError, match="Forbidden"):
        reference_data.validate_file(kind, {"items": [{"name": "x", "data_source": 1}]})


def test_webhook_secrets_and_credential_urls_are_rejected():
    with pytest.raises(ValueError, match="Forbidden"):
        reference_data.validate_file("webhooks", {"items": [{"name": "x", "secret": "never"}]})
    with pytest.raises(ValueError, match="credentials"):
        reference_data.validate_file("webhooks", {"items": [{"name": "x", "payload_url": "https://u:p@example.test/hook"}]})
    with pytest.raises(ValueError, match="query"):
        reference_data.validate_file("webhooks", {"items": [{"name": "x", "payload_url": "https://example.test/hook?token=x"}]})


def test_sensitive_headers_are_redacted():
    payload = reference_data.validate_file("webhooks", {"items": [{"name": "x", "additional_headers": "Authorization: Bearer never\nX-Trace: ok"}]})
    assert "never" not in str(payload)
    assert payload["items"][0]["additional_headers"] == "Authorization: <redacted>\nX-Trace: ok"


def test_diff_by_name():
    result = reference_data.diff_kind({"items": [{"name": "a", "slug": "old"}, {"name": "b"}]}, {"items": [{"name": "a", "slug": "new"}, {"name": "c"}]})
    assert result["missing_on_instance"] == ["b"]
    assert result["extra_on_instance"] == ["c"]
    assert result["changed"][0]["field_changes"][0]["field"] == "slug"


@pytest.mark.parametrize("path", ["../reference-data", "/reference-data", "a/../../b", r"a\b"])
def test_path_traversal_rejected(path):
    with pytest.raises(ValueError):
        reference_data.path_for(path, "tags")
