"""
Pydantic models mirroring NetBox's device-type YAML definition format,
as used by https://github.com/netbox-community/devicetype-library

These are intentionally permissive (most fields optional) since NetBox
itself tolerates partial definitions, and we want import of real-world
community YAML files to succeed even when they omit optional fields.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

WeightUnit = Literal["kg", "g", "lb", "oz"]


class ComponentTemplateBase(BaseModel):
    name: str
    label: Optional[str] = None
    description: Optional[str] = None


class InterfaceTemplate(ComponentTemplateBase):
    type: str = "other"
    mgmt_only: bool = False
    poe_mode: Optional[str] = None
    poe_type: Optional[str] = None


class ConsolePortTemplate(ComponentTemplateBase):
    type: str = "rj-45"


class ConsoleServerPortTemplate(ComponentTemplateBase):
    type: str = "rj-45"


class PowerPortTemplate(ComponentTemplateBase):
    type: str = "iec-60320-c14"
    maximum_draw: Optional[int] = None
    allocated_draw: Optional[int] = None


class PowerOutletTemplate(ComponentTemplateBase):
    type: str = "iec-60320-c13"
    power_port: Optional[str] = None
    feed_leg: Optional[str] = None


class RearPortTemplate(ComponentTemplateBase):
    type: str = "8p8c"
    positions: int = 1


class FrontPortTemplate(ComponentTemplateBase):
    type: str = "8p8c"
    rear_port: Optional[str] = None
    rear_port_position: int = 1


class DeviceBayTemplate(ComponentTemplateBase):
    pass


class ModuleBayTemplate(ComponentTemplateBase):
    position: Optional[str] = None


class DeviceType(BaseModel):
    manufacturer: str
    model: str
    slug: str
    part_number: Optional[str] = None
    u_height: float = 1
    is_full_depth: bool = True
    subdevice_role: Optional[Literal["parent", "child"]] = None
    airflow: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[WeightUnit] = None
    front_image: Optional[bool] = None
    rear_image: Optional[bool] = None
    comments: Optional[str] = None

    interfaces: list[InterfaceTemplate] = Field(default_factory=list)
    console_ports: list[ConsolePortTemplate] = Field(default_factory=list, alias="console-ports")
    console_server_ports: list[ConsoleServerPortTemplate] = Field(
        default_factory=list, alias="console-server-ports"
    )
    power_ports: list[PowerPortTemplate] = Field(default_factory=list, alias="power-ports")
    power_outlets: list[PowerOutletTemplate] = Field(default_factory=list, alias="power-outlets")
    rear_ports: list[RearPortTemplate] = Field(default_factory=list, alias="rear-ports")
    front_ports: list[FrontPortTemplate] = Field(default_factory=list, alias="front-ports")
    device_bays: list[DeviceBayTemplate] = Field(default_factory=list, alias="device-bays")
    module_bays: list[ModuleBayTemplate] = Field(default_factory=list, alias="module-bays")

    class Config:
        populate_by_name = True

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize back to the hyphenated keys NetBox/community YAML expects."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        return data


# Endpoint segment for each component list, as used by NetBox's DCIM API.
COMPONENT_ENDPOINTS = {
    "interfaces": "interface-templates",
    "console-ports": "console-port-templates",
    "console-server-ports": "console-server-port-templates",
    "power-ports": "power-port-templates",
    "power-outlets": "power-outlet-templates",
    "rear-ports": "rear-port-templates",
    "front-ports": "front-port-templates",
    "device-bays": "device-bay-templates",
    "module-bays": "module-bay-templates",
}

# Attributes beyond name/label/description that matter for diffing each
# component type, matching the fields each *Template model above actually
# defines. Kept alongside COMPONENT_ENDPOINTS since the two need to stay in
# sync with the schema classes above whenever a field is added there.
COMPONENT_EXTRA_FIELDS: dict[str, list[str]] = {
    "interfaces": ["mgmt_only", "poe_mode", "poe_type"],
    "console-ports": [],
    "console-server-ports": [],
    "power-ports": ["maximum_draw", "allocated_draw"],
    "power-outlets": ["power_port", "feed_leg"],
    "rear-ports": ["positions"],
    "front-ports": ["rear_port", "rear_port_position"],
    "device-bays": [],
    "module-bays": ["position"],
}
