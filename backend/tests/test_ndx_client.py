from app.services import ndx_client
import responses


def test_ndx_search_matches_manufacturer_model_and_part_number(monkeypatch):
    monkeypatch.setattr(ndx_client, "_index", lambda: [
        {"type": "device-type", "vendor_slug": "acme", "vendor_name": "Acme Corp",
         "manufacturer": "Acme", "model": "Router 9000", "slug": "router-9000",
         "part_number": "PN-42", "u_height": 1, "source": "netbox_labs"},
        {"type": "module-type", "vendor_slug": "acme", "vendor_name": "Acme Corp",
         "manufacturer": "Acme", "model": "Module", "slug": "module", "part_number": "PN-42"},
    ])
    assert ndx_client.search("acme router")[0]["slug"] == "router-9000"
    assert ndx_client.search("PN-42")[0]["part_number"] == "PN-42"
    assert len(ndx_client.search("PN-42")) == 1


def test_ndx_download_rejects_unsafe_identifiers():
    try:
        ndx_client.get_yaml("../vendor", "device")
    except ValueError as exc:
        assert "Invalid NDX" in str(exc)
    else:
        raise AssertionError("unsafe NDX identifier was accepted")


@responses.activate
def test_ndx_public_catalog_contract_sample():
    ndx_client._cache = None
    responses.get(ndx_client.INDEX_URL, json=[{
        "vendor_slug": "acme_vendor", "vendor_name": "Acme Vendor", "manufacturer": "Acme",
        "model": "Router 1.0", "slug": "router_1.0", "part_number": 12345,
        "type": "device-type", "source": "netbox_labs", "u_height": 1,
    }])
    responses.get(f"{ndx_client.BASE_URL}/acme_vendor/router_1.0/yaml", body=(
        "manufacturer: Acme\nmodel: Router 1.0\nslug: router_1.0\npart_number: 12345\n"
    ))
    row = ndx_client.search("12345")[0]
    assert row["part_number"] == "12345"
    payload = ndx_client.get_yaml(row["vendor_slug"], row["slug"])
    assert payload["slug"] == "router_1.0"
