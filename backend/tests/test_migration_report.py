import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent))
from test_migration_planner import FakeClient  # noqa: E402

from app import models  # noqa: E402
from app.services.migration.executor import execute_job  # noqa: E402
from app.services.migration.planner import build_plan, persist_plan  # noqa: E402
from app.services.migration.registry import load_registry  # noqa: E402
from app.services.migration.report import build_report, render_html, render_json  # noqa: E402

REGISTRY = load_registry()


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


def _make_instances(db):
    source = models.NetboxInstance(name="Source NB", base_url="https://source.example", api_token_encrypted="x")
    target = models.NetboxInstance(name="Target NB", base_url="https://target.example", api_token_encrypted="x")
    db.add_all([source, target])
    db.commit()
    return source, target


def _sample_plan_and_job(db):
    source_inst, target_inst = _make_instances(db)
    source = FakeClient({
        "dcim.sites": [
            {"id": 1, "slug": "Amsterdam 1", "name": "Amsterdam 1"},
            {"id": 2, "slug": "London 1", "name": "London 1"},
        ],
    }, read_only=True)
    target = FakeClient({"dcim.sites": [{"id": 100, "slug": "Amsterdam 1", "name": "Amsterdam 1"}]})
    plan = build_plan(registry=REGISTRY, source_client=source, target_client=target, selected_types={"dcim.site"}, tenant_filter=[], mapping_overrides={})
    job = models.MigrationJob(source_instance_id=source_inst.id, target_instance_id=target_inst.id, status="planned")
    db.add(job)
    db.commit()
    persist_plan(db, job.id, plan)
    return job, target


def test_dry_run_report_shows_predicted_actions_before_execution(db):
    job, target = _sample_plan_and_job(db)
    report = build_report(db, job, REGISTRY)

    assert report.has_run is False
    section = next(s for s in report.sections if s.type_key == "dcim.site")
    assert section.title == "Sites"
    assert "1 will be created" in section.summary or "will be created" in section.summary
    row_map = {r.source_natural_key: r for r in section.rows}
    assert row_map["Amsterdam 1"].action == "map"
    assert row_map["Amsterdam 1"].target_id == 100
    assert row_map["London 1"].action == "create"
    assert row_map["London 1"].target_id is None  # nothing created yet — this is still a preview


def test_report_source_and_target_links_use_registry_ui_path(db):
    job, target = _sample_plan_and_job(db)
    report = build_report(db, job, REGISTRY)
    section = next(s for s in report.sections if s.type_key == "dcim.site")
    row = next(r for r in section.rows if r.source_natural_key == "Amsterdam 1")
    assert row.source_link == "https://source.example/dcim/sites/1/"
    assert row.target_link == "https://target.example/dcim/sites/100/"


def test_report_after_execution_shows_real_outcomes(db):
    job, target = _sample_plan_and_job(db)
    execute_job(db, job, registry=REGISTRY, target_client=target)
    db.refresh(job)

    report = build_report(db, job, REGISTRY)
    assert report.has_run is True
    assert report.status == "completed"
    section = next(s for s in report.sections if s.type_key == "dcim.site")
    row_map = {r.source_natural_key: r for r in section.rows}
    assert row_map["London 1"].execution_status == "done"
    assert row_map["London 1"].target_id is not None
    assert row_map["London 1"].target_link is not None


def test_html_report_renders_without_error_and_contains_key_content(db):
    job, target = _sample_plan_and_job(db)
    report = build_report(db, job, REGISTRY)
    html = render_html(report)
    assert "<html" in html
    assert "Sites" in html
    assert "Amsterdam 1" in html
    assert "https://source.example/dcim/sites/1/" in html


def test_html_report_escapes_hostile_content():
    from app.services.migration.report import ReportData, ReportRow, ReportSection
    report = ReportData(
        job_id="j1", status="planned", phase="primary", source_name="Src", target_name="Tgt", warnings=[],
        sections=[ReportSection(
            type_key="dcim.site", title="Sites", summary="1 will be created",
            rows=[ReportRow(
                source_id=1, source_natural_key='<script>alert(1)</script>', source_link="https://x/1/",
                action="create", target_id=None, target_link=None, execution_status="pending",
                error_detail=None, match_detail=None,
            )],
        )],
    )
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_json_report_structure(db):
    job, target = _sample_plan_and_job(db)
    report = build_report(db, job, REGISTRY)
    data = render_json(report)
    assert data["job_id"] == job.id
    assert data["has_run"] is False
    site_section = next(s for s in data["sections"] if s["type"] == "dcim.site")
    assert {r["source_natural_key"] for r in site_section["rows"]} == {"Amsterdam 1", "London 1"}


def test_warnings_surfaced_at_top_level(db):
    job, target = _sample_plan_and_job(db)
    job.warnings_json = '["dcim.site id=2 (lon-1): dropped custom fields [\'circuit_id\']"]'
    db.commit()
    report = build_report(db, job, REGISTRY)
    assert len(report.warnings) == 1
    html = render_html(report)
    assert "dropped custom fields" in html
