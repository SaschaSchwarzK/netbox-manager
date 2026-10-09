from types import SimpleNamespace

from app.services.export import schema


class Client:
    def __init__(self, fields, vdc_error=None):
        self.fields = fields
        self.vdc_error = vdc_error
        endpoint = SimpleNamespace(filter=lambda **kwargs: [])
        self.nb = SimpleNamespace(
            extras=SimpleNamespace(custom_fields=object()),
            dcim=SimpleNamespace(devices=endpoint, virtual_device_contexts=endpoint),
            virtualization=SimpleNamespace(virtual_machines=endpoint),
        )

    def paginated(self, endpoint, **filters):
        yield from self.fields

    def call(self, fn):
        if self.vdc_error:
            raise self.vdc_error
        return fn()


def test_schema_supports_object_types_and_sorts_custom_fields():
    client = Client([
        {"name": "z", "label": "Zulu", "type": {"value": "text"}, "object_types": ["dcim.device"], "weight": 2},
        {"name": "a", "label": "Alpha", "type": "boolean", "object_types": [{"app_label": "dcim", "model": "device"}], "group_name": "Core", "weight": 1},
    ])
    result = schema.fetch_schema(client)
    device = result["object_types"][0]
    assert [field["name"] for field in device["custom_fields"]] == ["a", "z"]


def test_schema_supports_legacy_content_types():
    result = schema.fetch_schema(Client([{"name": "legacy", "content_types": ["virtualization.virtualmachine"]}]))
    vm = next(item for item in result["object_types"] if item["key"] == "virtualmachine")
    assert vm["custom_fields"][0]["name"] == "legacy"


def test_schema_cache_avoids_second_fetch():
    schema.clear_cache()
    client = Client([])
    first = schema.cached_schema("one", client)
    client.fields = [{"name": "later", "object_types": ["dcim.device"]}]
    assert schema.cached_schema("one", client) is first
