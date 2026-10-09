from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from app.config import settings
from app.services.export import storage
from app.services.export.schema import TYPES

PLAIN_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")
INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_tempfile_lock = threading.Lock()


class ExportCancelled(RuntimeError):
    pass


class ExportLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class JobSpec:
    id: str
    tenant_id: int
    object_types: list[str]
    fields: dict[str, dict[str, list[str]]]
    format: str
    delimiter: str = ","


@dataclass(frozen=True)
class ExportResult:
    file_name: str
    file_size: int
    row_counts: dict[str, int]


@contextmanager
def _openpyxl_tempdir():
    # openpyxl write-only worksheets use global tempfile state. Serialize workbook
    # creation briefly so sheet XML stays on the export volume, never /tmp.
    with _tempfile_lock:
        previous = tempfile.tempdir
        tempfile.tempdir = str(Path(settings.export_dir) / "tmp")
        try:
            yield
        finally:
            tempfile.tempdir = previous


def safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = ILLEGAL_CHARACTERS_RE.sub("", text)[:32767]
    if text.startswith(INJECTION_PREFIXES) and not PLAIN_NUMBER.fullmatch(text):
        return "'" + text
    return text


def custom_value(value: Any, field_type: str) -> str:
    if value is None:
        return ""
    if field_type == "boolean":
        return "true" if bool(value) else "false"
    if field_type in {"select", "selection"}:
        return safe_cell(value.get("label", value.get("value", "")) if isinstance(value, dict) else value)
    if field_type == "multiselect":
        return safe_cell("; ".join(str(item.get("label", item.get("value", "")) if isinstance(item, dict) else item) for item in value))
    if field_type == "object":
        return safe_cell(_nested(value))
    if field_type == "multiobject":
        return safe_cell("; ".join(_nested(item) for item in value))
    if field_type == "json" or isinstance(value, (dict, list)):
        return safe_cell(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return safe_cell(value)


def _nested(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display") or "")
    return str(value or "")


def _endpoint(client, type_key: str):
    definition = TYPES[type_key]
    return getattr(getattr(client.nb, definition["endpoint"][0]), definition["endpoint"][1])


def _lookup(client, endpoint, fields: str) -> dict[int, dict]:
    return {int(row["id"]): row for row in client.paginated(endpoint, fields=fields, limit=500)}


def _columns(spec: JobSpec, type_key: str) -> tuple[list[str], list[dict]]:
    definition = TYPES[type_key]
    selected = spec.fields.get(type_key, {})
    optional = set(selected.get("optional", []))
    custom = selected.get("custom_fields", [])
    columns = [item[0] for item in definition["fixed"]]
    columns += [item[0] for item in definition["optional"] if item[0] in optional]
    columns += [f"cf_{item['name']}" for item in custom]
    return columns, custom


def _record_row(record: dict, columns: list[str], custom: list[dict], regions: dict, manufacturers: dict) -> list[str]:
    values: dict[str, Any] = {
        "name": record.get("name"), "primary_ip": (record.get("primary_ip") or {}).get("address"),
        "serial": record.get("serial"), "site": _nested(record.get("site")),
        "device_type": _nested(record.get("device_type")), "virtual_chassis": _nested(record.get("virtual_chassis")),
        "primary_device": _nested(record.get("device")), "cluster": _nested(record.get("cluster")),
    }
    site_id = (record.get("site") or {}).get("id") if isinstance(record.get("site"), dict) else None
    values["region"] = regions.get(site_id, "")
    device_type = record.get("device_type") or {}
    nested_manufacturer = device_type.get("manufacturer") if isinstance(device_type, dict) else None
    values["manufacturer"] = (
        _nested(nested_manufacturer) or manufacturers.get(device_type.get("id"), "")
        if isinstance(device_type, dict)
        else ""
    )
    custom_values = record.get("custom_fields") or {}
    for field in custom:
        values[f"cf_{field['name']}"] = custom_value(custom_values.get(field["name"]), field.get("type", "text"))
    return [safe_cell(values.get(column, "")) for column in columns]


def _rows(spec: JobSpec, client, type_key: str, progress, is_cancelled):
    selected = set(spec.fields.get(type_key, {}).get("optional", []))
    regions, manufacturers = {}, {}
    if "region" in selected:
        sites = _lookup(client, client.nb.dcim.sites, "id,region")
        regions = {key: _nested(value.get("region")) for key, value in sites.items()}
    if type_key == "device" and "manufacturer" in selected:
        device_types = _lookup(client, client.nb.dcim.device_types, "id,manufacturer")
        manufacturers = {key: _nested(value.get("manufacturer")) for key, value in device_types.items()}
    columns, custom = _columns(spec, type_key)
    top_fields = {"id", "name", "primary_ip", "custom_fields"}
    source_by_column = {item[0]: item[2].split(".")[0] for item in TYPES[type_key]["optional"]}
    top_fields.update(source_by_column[key] for key in selected if key in source_by_column)
    if type_key == "device":
        top_fields.add("serial")
    done, total = 0, 0

    def on_page(page):
        nonlocal total
        if not total:
            total = int(page.get("count", 0))

    for record in client.paginated(_endpoint(client, type_key), tenant_id=spec.tenant_id, limit=500,
                                   ordering="id", fields=",".join(sorted(top_fields)), on_page=on_page):
        if is_cancelled():
            raise ExportCancelled()
        done += 1
        if done > settings.export_max_rows_per_type:
            raise ExportLimitExceeded(f"More than {settings.export_max_rows_per_type} rows for {type_key}; narrow the export.")
        yield columns, _record_row(record, columns, custom, regions, manufacturers)
        progress(type_key, done, total)


def run_export(spec: JobSpec, client, progress: Callable[[str, int, int], None], is_cancelled: Callable[[], bool]) -> ExportResult:
    storage.ensure_dirs()
    temporary: list[Path] = []
    row_counts: dict[str, int] = {}
    try:
        if spec.format == "xlsx":
            output = storage.tmp_path(spec.id, "xlsx")
            temporary.append(output)
            with _openpyxl_tempdir():
                workbook = Workbook(write_only=True)
                for type_key in spec.object_types:
                    sheet = workbook.create_sheet(TYPES[type_key]["sheet"])
                    sheet.freeze_panes = "A2"
                    count = 0
                    for columns, row in _rows(spec, client, type_key, progress, is_cancelled):
                        if count == 0:
                            header = [WriteOnlyCell(sheet, value=value) for value in columns]
                            for cell in header:
                                cell.font = Font(bold=True)
                            sheet.append(header)
                        sheet.append(row)
                        count += 1
                    if count == 0:
                        columns, _ = _columns(spec, type_key)
                        sheet.append(columns)
                    row_counts[type_key] = count
                if "Sheet" in workbook.sheetnames:
                    del workbook["Sheet"]
                workbook.save(output)
            extension = "xlsx"
        else:
            csv_paths = []
            for type_key in spec.object_types:
                output = storage.tmp_path(spec.id, "csv", f"-{type_key}")
                temporary.append(output)
                csv_paths.append((type_key, output))
                count = 0
                with output.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream, delimiter=spec.delimiter)
                    wrote_header = False
                    for columns, row in _rows(spec, client, type_key, progress, is_cancelled):
                        if not wrote_header:
                            writer.writerow(columns)
                            wrote_header = True
                        writer.writerow(row)
                        count += 1
                    if not wrote_header:
                        columns, _ = _columns(spec, type_key)
                        writer.writerow(columns)
                row_counts[type_key] = count
            if len(csv_paths) == 1:
                output = csv_paths[0][1]
                extension = "csv"
            else:
                output = storage.tmp_path(spec.id, "zip")
                temporary.append(output)
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    for type_key, csv_path in csv_paths:
                        archive.write(csv_path, TYPES[type_key]["csv"])
                extension = "zip"
        final = storage.final_path(spec.id, extension)
        os.replace(output, final)
        return ExportResult(final.name, final.stat().st_size, row_counts)
    finally:
        for path in temporary:
            if path.exists():
                path.unlink()
