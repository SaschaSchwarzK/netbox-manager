"""
Renders a MigrationJob as a human-readable report: a narrative summary per
object type ("3 sites will be created, 1 mapped to an existing site"), a
detail table with source/target links, and a warnings section.

Deliberately has NO separate "preview" vs "result" code path: it queries
MigrationJobItem/MigrationJobPatch exactly as they stand. Called right after
planning, target_id is still null and execution_status is "pending" for
every create/update row, so the table reads as a prediction ("will be
created"); called after execution, the same query returns the real target
ids and final statuses. See services/migration/planner.py and executor.py —
this module writes nothing and reads only what they already produced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MigrationJob, MigrationJobItem, MigrationJobPatch, NetboxInstance
from app.services.migration.registry import Registry

_DISPLAY_NAMES = {
    "tenancy.tenantgroup": "Tenant Groups", "tenancy.tenant": "Tenants",
    "dcim.region": "Regions", "dcim.sitegroup": "Site Groups", "dcim.site": "Sites",
    "dcim.location": "Locations", "dcim.rackrole": "Rack Roles", "dcim.rack": "Racks",
    "dcim.manufacturer": "Manufacturers", "dcim.devicetype": "Device Types",
    "dcim.moduletype": "Module Types", "dcim.platform": "Platforms", "dcim.devicerole": "Device Roles",
    "virtualization.clustertype": "Cluster Types", "virtualization.clustergroup": "Cluster Groups",
    "virtualization.cluster": "Clusters", "ipam.rir": "RIRs", "ipam.role": "IPAM Roles",
    "ipam.vrf": "VRFs", "ipam.vlangroup": "VLAN Groups", "dcim.device": "Devices",
    "dcim.virtualchassis": "Virtual Chassis", "dcim.virtualdevicecontext": "Virtual Device Contexts",
    "dcim.modulebay": "Module Bays", "dcim.module": "Modules",
    "dcim.interface": "Interfaces", "dcim.consoleport": "Console Ports",
    "dcim.consoleserverport": "Console Server Ports", "dcim.powerport": "Power Ports",
    "dcim.poweroutlet": "Power Outlets", "dcim.frontport": "Front Ports", "dcim.rearport": "Rear Ports",
    "dcim.devicebay": "Device Bays", "dcim.inventoryitem": "Inventory Items",
    "virtualization.virtualmachine": "Virtual Machines", "virtualization.vminterface": "VM Interfaces",
    "virtualization.virtualdisk": "Virtual Disks", "ipam.aggregate": "Aggregates", "ipam.prefix": "Prefixes",
    "ipam.iprange": "IP Ranges", "ipam.ipaddress": "IP Addresses", "ipam.vlan": "VLANs",
    "ipam.asn": "ASNs", "ipam.fhrpgroup": "FHRP Groups",
    "ipam.fhrpgroupassignment": "FHRP Group Assignments", "ipam.service": "Services",
    "dcim.macaddress": "MAC Addresses", "extras.configtemplate": "Config Templates",
    "ipam.vlantranslationpolicy": "VLAN Translation Policies",
    "ipam.vlantranslationrule": "VLAN Translation Rules", "ipam.asnrange": "ASN Ranges",
}

_ACTION_LABELS = {
    "create": "will be created", "update": "will be updated", "map": "mapped to an existing object",
    "skip": "skipped", "ambiguous": "ambiguous — needs manual mapping",
}


def display_name(type_key: str) -> str:
    return _DISPLAY_NAMES.get(type_key, type_key.split(".")[-1].replace("_", " ").title())


@dataclass
class ReportRow:
    source_id: int
    source_natural_key: str
    source_link: str
    action: str
    target_id: int | None
    target_link: str | None
    execution_status: str
    error_detail: str | None
    match_detail: str | None
    target_natural_key: str | None = None


@dataclass
class ReportSection:
    type_key: str
    title: str
    summary: str
    rows: list[ReportRow] = field(default_factory=list)


@dataclass
class ReportData:
    job_id: str
    status: str
    phase: str
    source_name: str
    target_name: str
    warnings: list[str]
    sections: list[ReportSection] = field(default_factory=list)
    has_run: bool = False  # True once execution has started — distinguishes "will be" from "was" phrasing


def _instance_link(base_url: str, ui_path: str, object_id: int) -> str:
    return f"{base_url.rstrip('/')}/{ui_path}/{object_id}/"


def build_report(db: Session, job: MigrationJob, registry: Registry) -> ReportData:
    source = db.get(NetboxInstance, job.source_instance_id)
    target = db.get(NetboxInstance, job.target_instance_id)
    items = db.execute(
        select(MigrationJobItem).where(MigrationJobItem.job_id == job.id).order_by(MigrationJobItem.order_index)
    ).scalars().all()
    patches_by_item = {}
    for patch in db.execute(
        select(MigrationJobPatch).where(MigrationJobPatch.job_id == job.id).order_by(MigrationJobPatch.order_index)
    ).scalars():
        patches_by_item.setdefault((patch.object_type, patch.source_id), []).append(patch)

    warnings = json.loads(job.warnings_json or "[]")
    has_run = job.status not in ("planning", "planned") and not (
        job.status == "failed" and job.started_at is None
    )

    by_type: dict[str, list[MigrationJobItem]] = {}
    for item in items:
        by_type.setdefault(item.object_type, []).append(item)

    sections: list[ReportSection] = []
    for type_key in by_type:  # preserves order_index-derived (topological) order, since `items` is already sorted
        type_spec = registry[type_key] if type_key in registry else None
        ui_path = type_spec.ui_path if type_spec else type_key.replace(".", "/")
        rows = []
        for item in by_type[type_key]:
            source_link = _instance_link(source.base_url, ui_path, item.source_id) if source else ""
            target_link = _instance_link(target.base_url, ui_path, item.target_id) if target and item.target_id and item.target_id > 0 else None
            item_patches = patches_by_item.get((item.object_type, item.source_id), [])
            note_parts = [item.match_detail] if item.match_detail else []
            for p in item_patches:
                fields = ", ".join(json.loads(p.patch_fields_json))
                note_parts.append(f"patch for [{fields}]: {p.execution_status}" + (f" ({p.error_detail})" if p.error_detail else ""))
            rows.append(ReportRow(
                source_id=item.source_id, source_natural_key=item.source_natural_key, source_link=source_link,
                action=item.planned_action, target_id=item.target_id if item.target_id and item.target_id > 0 else None,
                target_natural_key=item.target_natural_key,
                target_link=target_link, execution_status=item.execution_status,
                error_detail=item.error_detail, match_detail="; ".join(note_parts) or None,
            ))
        sections.append(ReportSection(
            type_key=type_key, title=display_name(type_key),
            summary=_summarize_section(by_type[type_key], has_run=has_run), rows=rows,
        ))

    return ReportData(
        job_id=job.id, status=job.status, phase=job.phase,
        source_name=source.name if source else job.source_instance_id,
        target_name=target.name if target else job.target_instance_id,
        warnings=warnings, sections=sections, has_run=has_run,
    )


def _summarize_section(items: list[MigrationJobItem], *, has_run: bool) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.planned_action] = counts.get(item.planned_action, 0) + 1
    parts = []
    for action in ("create", "update", "map", "skip", "ambiguous"):
        n = counts.get(action, 0)
        if not n:
            continue
        if has_run and action in ("create", "update"):
            done = sum(1 for i in items if i.planned_action == action and i.execution_status == "done")
            errored = sum(1 for i in items if i.planned_action == action and i.execution_status == "error")
            verb = "created" if action == "create" else "updated"
            detail = f"{done} {verb}" + (f", {errored} failed" if errored else "")
            parts.append(f"{n} {detail}" if errored else detail if n == done else f"{n} {detail}")
        else:
            parts.append(f"{n} {_ACTION_LABELS[action]}")
    return "; ".join(parts) if parts else "nothing to do"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STATUS_ICON = {"pending": "⏳", "done": "✓", "error": "✗"}


def render_html(report: ReportData) -> str:
    warnings_html = ""
    if report.warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in report.warnings)
        warnings_html = f'<div class="warnings"><h2>Warnings</h2><ul>{items}</ul></div>'

    sections_html = []
    for section in report.sections:
        rows_html = []
        for row in section.rows:
            source_cell = f'<a href="{escape(row.source_link)}" target="_blank">{escape(row.source_natural_key)}</a>' if row.source_link else escape(row.source_natural_key)
            if row.target_link:
                target_text = f"{row.target_natural_key} (#{row.target_id})" if row.target_natural_key else f"#{row.target_id}"
                target_cell = f'<a href="{escape(row.target_link)}" target="_blank" title="Target ID #{row.target_id}">{escape(target_text)}</a>'
            elif row.target_id:
                target_cell = escape(f"{row.target_natural_key} (#{row.target_id})" if row.target_natural_key else f"#{row.target_id}")
            else:
                target_cell = "—"
            icon = _STATUS_ICON.get(row.execution_status, "")
            action_label = _ACTION_LABELS.get(row.action, row.action)
            note = escape(row.error_detail) if row.error_detail else (escape(row.match_detail) if row.match_detail else "")
            row_class = "row-error" if row.execution_status == "error" or row.action == "ambiguous" else ""
            rows_html.append(
                f'<tr class="{row_class}"><td>{icon}</td><td>{source_cell}</td>'
                f'<td>{escape(action_label)}</td><td>{target_cell}</td><td class="note">{note}</td></tr>'
            )
        sections_html.append(f"""
        <section>
          <h2>{escape(section.title)} <span class="count">({len(section.rows)})</span></h2>
          <p class="summary">{escape(section.summary)}</p>
          <table>
            <thead><tr><th></th><th>Source</th><th>Action</th><th>Target</th><th>Notes</th></tr></thead>
            <tbody>{''.join(rows_html)}</tbody>
          </table>
        </section>""")

    title = "Migration dry-run preview" if not report.has_run else f"Migration report ({report.status})"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 2rem auto; max-width: 900px; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }}
  .count {{ color: #888; font-weight: normal; }}
  .summary {{ color: #444; margin: 0.25rem 0 0.75rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th, td {{ text-align: left; padding: 0.3rem 0.5rem; border-bottom: 1px solid #eee; }}
  th {{ color: #666; font-weight: 600; }}
  tr.row-error {{ background: #fff2f2; }}
  td.note {{ color: #a33; font-size: 0.85rem; }}
  .warnings {{ background: #fff8e6; border: 1px solid #f0d98a; border-radius: 6px; padding: 0.75rem 1rem; margin-bottom: 1.5rem; }}
  .warnings h2 {{ margin-top: 0; border: none; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
  <h1>{escape(title)}: {escape(report.source_name)} → {escape(report.target_name)}</h1>
  {warnings_html}
  {''.join(sections_html)}
</body>
</html>"""


def render_json(report: ReportData) -> dict:
    return {
        "job_id": report.job_id, "status": report.status, "phase": report.phase,
        "source": report.source_name, "target": report.target_name,
        "has_run": report.has_run, "warnings": report.warnings,
        "sections": [
            {
                "type": s.type_key, "title": s.title, "summary": s.summary,
                "rows": [
                    {
                        "source_id": r.source_id, "source_natural_key": r.source_natural_key,
                        "source_link": r.source_link, "action": r.action, "target_id": r.target_id,
                        "target_natural_key": r.target_natural_key,
                        "target_link": r.target_link, "execution_status": r.execution_status,
                        "error_detail": r.error_detail, "note": r.match_detail,
                    }
                    for r in s.rows
                ],
            }
            for s in report.sections
        ],
    }
