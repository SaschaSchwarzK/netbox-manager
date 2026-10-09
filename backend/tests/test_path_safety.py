import pytest
from pydantic import ValidationError

from app import schemas
from app.devicetype_schema import DeviceType
from app.path_safety import validate_path_pattern, validate_repo_path, validate_segment


@pytest.mark.parametrize("value", ["", "..", "../vendor", "vendor/name", "vendor\\name", ".hidden", "bad\x00name"])
def test_unsafe_segments_are_rejected(value):
    with pytest.raises(ValueError):
        validate_segment(value)


def test_spaces_and_unicode_are_valid_segments():
    assert validate_segment("Palo Alto Networks Ü") == "Palo Alto Networks Ü"


@pytest.mark.parametrize(
    "path",
    ["../../.github/workflows/x.yml", "/device-types/A/x.yml", "device-types/../x.yml",
     "module-types/A/x.yml", "device-types/A/x.json", "device-types\\A\\x.yml"],
)
def test_unsafe_repository_paths_are_rejected(path):
    with pytest.raises(ValueError):
        validate_repo_path(path, "device-types", (".yml", ".yaml"))


def test_valid_repository_path_allows_manufacturer_spaces():
    assert validate_repo_path(
        "device-types/Palo Alto Networks/pa-220.yml", "device-types", (".yml", ".yaml")
    ) == "device-types/Palo Alto Networks/pa-220.yml"


@pytest.mark.parametrize("pattern", ["device-types/{unknown}/{slug}.yml", "device-types/{}/x.yml",
                                      "device-types/{0}/x.yml", "device-types/{manufacturer.name}/x.yml",
                                      "device-types/{manufacturer[0]}/x.yml"])
def test_unsafe_path_patterns_are_rejected(pattern):
    with pytest.raises(ValueError):
        validate_path_pattern(pattern, {"manufacturer", "model", "slug"})


def test_github_target_schema_rejects_unknown_pattern_placeholder():
    with pytest.raises(ValidationError):
        schemas.GithubTargetCreate(name="x", repo="o/r", pat="token",
                                   path_pattern="device-types/{vendor}/{slug}.yml")


@pytest.mark.parametrize("slug", ["bad slug", "bad/slug", "ümlaut"])
def test_device_type_slug_matches_netbox_rules(slug):
    with pytest.raises(ValidationError):
        DeviceType(manufacturer="Vendor", model="Model", slug=slug)
