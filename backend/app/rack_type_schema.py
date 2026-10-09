"""Pydantic representation of the community devicetype-library rack-type format."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.devicetype_schema import WeightUnit

RackFormFactor = Literal[
    "wall-cabinet", "4-post-frame", "2-post-frame", "4-post-cabinet",
    "wall-frame", "wall-frame-vertical", "wall-cabinet-vertical",
]


class RackType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manufacturer: str
    model: str
    slug: str = Field(pattern=r"^[-a-zA-Z0-9_]+$")
    description: Optional[str] = Field(default=None, max_length=200)
    form_factor: RackFormFactor
    width: Literal[10, 19, 20, 23]
    u_height: int = Field(ge=0)
    starting_unit: int = Field(ge=1)
    outer_width: Optional[int] = Field(default=None, ge=0)
    outer_height: Optional[int] = Field(default=None, ge=0)
    outer_depth: Optional[int] = Field(default=None, ge=0)
    outer_unit: Optional[Literal["mm", "in"]] = None
    weight: Optional[float] = Field(default=None, ge=0, multiple_of=0.01)
    max_weight: Optional[int] = Field(default=None, ge=0)
    weight_unit: Optional[WeightUnit] = None
    mounting_depth: Optional[int] = Field(default=None, ge=0)
    desc_units: bool = False
    comments: Optional[str] = None

    def to_internal_dict(self) -> dict:
        return self.model_dump(exclude_none=True)

    def to_yaml_dict(self) -> dict:
        return self.to_internal_dict()
