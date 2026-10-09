from types import SimpleNamespace

from app.services import diff, netbox_client


class Endpoint:
    def __init__(self, rows):
        self.rows = rows

    def get(self, **filters):
        for row in self.rows:
            matches = True
            for key, value in filters.items():
                actual = getattr(row, key.removesuffix("_id"), None)
                actual = getattr(actual, "id", actual) if key.endswith("_id") else actual
                matches = matches and actual == value
            if matches:
                return row
        return None

    def filter(self, **filters):
        ids = filters.get("id") or filters.get("device_type_id")
        if ids is None:
            return self.rows
        ids = ids if isinstance(ids, list) else [ids]
        result = []
        for row in self.rows:
            value = row.id if "id" in filters else getattr(row.device_type, "id", row.device_type)
            if value in ids:
                result.append(row)
        return result


def test_bulk_and_single_component_reads_produce_identical_diffs(monkeypatch):
    manufacturer = SimpleNamespace(id=9, name="Vendor", __str__=lambda self: self.name)
    device_type = SimpleNamespace(
        id=1, manufacturer=manufacturer, model="Switch", slug="switch", part_number=None,
        u_height=1, is_full_depth=True, subdevice_role=None, weight=None, weight_unit=None,
        comments="", custom_fields={}, front_image=None, rear_image=None,
    )
    interface = SimpleNamespace(
        id=11, device_type=SimpleNamespace(id=1), name="Ethernet1", type="1000base-t",
        label="", description="", mgmt_only=False, poe_mode=None, poe_type=None, rf_role=None,
    )
    empty = Endpoint([])
    endpoints = {name.replace("-", "_"): empty for name in netbox_client.COMPONENT_ENDPOINTS.values()}
    endpoints["interface_templates"] = Endpoint([interface])
    dcim = SimpleNamespace(
        manufacturers=SimpleNamespace(get=lambda **filters: manufacturer),
        device_types=Endpoint([device_type]),
        **endpoints,
    )
    monkeypatch.setattr(netbox_client, "get_client", lambda *args: SimpleNamespace(dcim=dcim))

    single = netbox_client.get_existing_device_type("url", "token", True, "Vendor", "switch")
    by_id = netbox_client.get_existing_device_type_by_id("url", "token", True, 1)
    bulk = netbox_client.get_existing_device_types_bulk("url", "token", True, [1])[1]
    source = {"manufacturer": "Vendor", "model": "Switch", "slug": "switch", "interfaces": [
        {"name": "Ethernet1", "type": "1000base-t", "mgmt_only": False},
    ]}

    assert bulk == single
    assert by_id == single
    assert diff.diff_payloads(source, bulk) == diff.diff_payloads(source, single)
