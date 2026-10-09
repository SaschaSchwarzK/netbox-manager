"""Pydantic representation of the community devicetype-library module-type format."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.devicetype_schema import (
    ComponentTemplateBase,
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    InterfaceTemplate,
    PowerOutletTemplate,
    PowerPortTemplate,
    WeightUnit,
)


class ModuleRearPortTemplate(ComponentTemplateBase):
    type: str = "8p8c"
    positions: int = 1
    color: Optional[str] = None


class ModuleFrontPortTemplate(ComponentTemplateBase):
    type: str = "8p8c"
    positions: int = 1
    color: Optional[str] = None


class ModuleBayTemplate(ComponentTemplateBase):
    position: Optional[str] = None


class PortMapping(BaseModel):
    front_port: str
    front_port_position: int = 1
    rear_port: str
    rear_port_position: int = 1


ModuleProfile = Literal["CPU", "Fan", "GPU", "Hard disk", "Memory", "Power supply", "Expansion card"]


class ModuleType(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    manufacturer: str
    model: str
    part_number: Optional[str] = None
    airflow: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[WeightUnit] = None
    description: Optional[str] = None
    comments: Optional[str] = None
    profile: Optional[ModuleProfile] = None
    attribute_data: dict[str, Any] = Field(default_factory=dict)

    interfaces: list[InterfaceTemplate] = Field(default_factory=list)
    console_ports: list[ConsolePortTemplate] = Field(default_factory=list, alias="console-ports")
    console_server_ports: list[ConsoleServerPortTemplate] = Field(default_factory=list, alias="console-server-ports")
    power_ports: list[PowerPortTemplate] = Field(default_factory=list, alias="power-ports")
    power_outlets: list[PowerOutletTemplate] = Field(default_factory=list, alias="power-outlets")
    rear_ports: list[ModuleRearPortTemplate] = Field(default_factory=list, alias="rear-ports")
    front_ports: list[ModuleFrontPortTemplate] = Field(default_factory=list, alias="front-ports")
    port_mappings: list[PortMapping] = Field(default_factory=list, alias="port-mappings")
    module_bays: list[ModuleBayTemplate] = Field(default_factory=list, alias="module-bays")

    def to_internal_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.to_internal_dict()


MODULE_COMPONENT_ENDPOINTS = {
    "interfaces": "interface-templates",
    "console-ports": "console-port-templates",
    "console-server-ports": "console-server-port-templates",
    "power-ports": "power-port-templates",
    "power-outlets": "power-outlet-templates",
    "rear-ports": "rear-port-templates",
    "front-ports": "front-port-templates",
    "module-bays": "module-bay-templates",
}

MODULE_COMPONENT_EXTRA_FIELDS = {
    "interfaces": ["mgmt_only", "poe_mode", "poe_type"],
    "console-ports": [], "console-server-ports": [],
    "power-ports": ["maximum_draw", "allocated_draw"],
    "power-outlets": ["power_port", "feed_leg"],
    "rear-ports": ["positions", "color"],
    "front-ports": ["positions", "color"],
    "module-bays": ["position"],
}
