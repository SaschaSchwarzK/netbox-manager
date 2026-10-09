"""
Pydantic models for the custom-fields template: a single YAML file per repo
listing every custom field and custom field choice set that should exist
across the fleet, mirroring NetBox's own /api/extras/custom-fields/ and
/api/extras/custom-field-choice-sets/ shapes closely enough to round-trip.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class CustomFieldChoiceSet(BaseModel):
    name: str
    description: Optional[str] = None
    extra_choices: list[list[str]] = Field(default_factory=list)  # [[value, label], ...] — these ARE the individual "custom field choices"
    base_choices: Optional[str] = None  # references one of NetBox's predefined choice sets (e.g. a built-in enum), instead of/alongside extra_choices
    order_alphabetically: bool = False


class CustomField(BaseModel):
    name: str
    label: Optional[str] = None
    type: str = "text"  # text, longtext, integer, decimal, boolean, date, datetime, url, json, select, multiselect, object, multiobject
    object_types: list[str] = Field(default_factory=list)  # e.g. ["dcim.device", "dcim.rack"]
    description: Optional[str] = None
    required: bool = False
    unique: bool = False
    search_weight: Optional[int] = 1000
    default: Optional[Any] = None
    choice_set: Optional[str] = None  # references a CustomFieldChoiceSet by name (select/multiselect only)
    related_object_type: Optional[str] = None  # e.g. "dcim.device" (object/multiobject only)
    related_object_filter: Optional[dict[str, Any]] = None  # object/multiobject only
    filter_logic: Optional[str] = "loose"
    weight: Optional[int] = 100
    group_name: Optional[str] = None
    ui_visible: Optional[str] = "always"
    ui_editable: Optional[str] = "yes"
    is_cloneable: bool = False
    validation_minimum: Optional[int] = None
    validation_maximum: Optional[int] = None
    validation_regex: Optional[str] = None
    comments: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def reject_pre_4_scope_name(cls, value):
        if isinstance(value, dict) and "content_types" in value:
            raise ValueError("content_types is unsupported; use object_types (NetBox >= 4.6.8).")
        return value


class CustomFieldsTemplate(BaseModel):
    custom_field_choice_sets: list[CustomFieldChoiceSet] = Field(default_factory=list)
    custom_fields: list[CustomField] = Field(default_factory=list)

    def to_yaml_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)
