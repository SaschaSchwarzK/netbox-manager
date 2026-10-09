from __future__ import annotations

import argparse
import json
import resource
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_STORED, ZipFile

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import crypto, models
from app.database import Base
from app.observability import get_request_stats, install_db_instrumentation, reset_request_stats, start_request_stats
from app.services import drift, github_repo, netbox_client
from app.services.migration.client import RateLimitedClient
from app.services.migration.executor import execute_job
from app.services.migration.registry import load_registry
from bench.fake_github import FakeGithubRepo
from bench.fake_netbox import FakeNetBox


class StreamedFileResponse:
    status_code = 200
    headers = {}

    def __init__(self, path): self.path = path
    def raise_for_status(self): return None
    def iter_content(self, chunk_size):
        with open(self.path, "rb") as stream:
            while chunk := stream.read(chunk_size): yield chunk


@contextmanager
def measured(name, github=None, netboxes=(), extra_metrics=None):
    token = start_request_stats(); tracemalloc.start(); started = time.perf_counter()
    github_before = github.total_requests if github else 0
    netbox_before = sum(item.total_requests for item in netboxes)
    try:
        yield
    finally:
        wall = time.perf_counter() - started
        result = {
            "scenario": name, "wall_seconds": round(wall, 4),
            "github_calls": (github.total_requests - github_before) if github else 0,
            "netbox_calls": sum(item.total_requests for item in netboxes) - netbox_before,
            "db_queries": get_request_stats().db_count,
            "peak_tracemalloc_mib": round(tracemalloc.get_traced_memory()[1] / 1048576, 2),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if Path('/proc').exists() else 1048576), 2),
        }
        if extra_metrics:
            result.update(extra_metrics)
        _current_results.append(result)
        tracemalloc.stop(); reset_request_stats(token)


def run(args):
    global _current_results
    _current_results = []
    github = FakeGithubRepo(count=args.repo_size, latency=args.latency)
    netboxes = [FakeNetBox(args.latency).start() for _ in range(10)]
    try:
        metadata = [{"path": path, "manufacturer": "Vendor", "slug": path.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                     "model": None, "part_number": None} for path in github.files]
        with patch.object(github_repo, "_repo", return_value=github):
            database = tempfile.NamedTemporaryFile(suffix=".db", delete=False); database.close()
            drift_engine = create_engine(f"sqlite:///{database.name}", connect_args={"check_same_thread": False})
            install_db_instrumentation(drift_engine); Base.metadata.create_all(drift_engine)
            DriftSession = sessionmaker(bind=drift_engine)
            db = DriftSession()
            try:
                target = models.GithubTarget(name="Benchmark", repo="owner/repo", branch="main",
                    path_pattern="device-types/{manufacturer}/{slug}.yml", pat_encrypted=crypto.encrypt("pat"))
                db.add(target)
                instances = []
                for index, fake in enumerate(netboxes[:3]):
                    instance = models.NetboxInstance(name=f"Instance {index}", base_url=fake.base_url,
                        api_token_encrypted=crypto.encrypt("token"), verify_ssl=False)
                    db.add(instance); instances.append(instance)
                db.flush()
                paths = list(github.files)[:200]
                db.add_all([models.DeviceTypePushHistory(repo_target_id=target.id, file_path=path,
                    target_type="netbox", target_name=instance.name, status="success")
                    for instance in instances for path in paths])
                db.commit()
                with patch.object(drift, "SessionLocal", DriftSession, create=True), \
                     patch.object(github_repo, "scan_device_type_metadata", return_value=metadata):
                    with measured("1 scheduled drift: 3x200 types", github, netboxes[:3]):
                        drift.run_full_check(db)
            finally:
                db.close(); drift_engine.dispose(); Path(database.name).unlink(missing_ok=True)

            switch = github_repo.get_file("pat", "owner/repo", "main",
                                          "device-types/Vendor/switch-0000.yml")["payload"]
            with measured("2 diff/push 48-port switch to 3 instances", github, netboxes[:3]):
                for instance in netboxes[:3]:
                    netbox_client.get_existing_device_type(instance.base_url, "token", False, "Vendor", "switch-0000")
                    netbox_client.push_device_type(instance.base_url, "token", False, switch, overwrite=True)

            with measured("3 global search: 5 instances", netboxes=netboxes[:5]):
                for instance in netboxes[:5]:
                    netbox_client.search_instance(instance.base_url, "token", False, "router")

            with measured("4 fleet: 10 instances", netboxes=netboxes):
                for instance in netboxes:
                    netbox_client.get_health(instance.base_url, "token", False)
                    netbox_client.check_token_expiry(instance.base_url, "token", False)

            with measured("5 bulk scan 2000/import 50", github):
                rows = github_repo.scan_repository_paths("pat", "owner/repo", "main", "device-types")
                files = [{"path": row["path"].replace("device-types/", "imported/", 1),
                          "payload": github_repo.get_file("pat", "owner/repo", "main", row["path"])["payload"]}
                         for row in rows[:50]]
                github_repo.bulk_create_files("pat", "owner/repo", "main", "bench-import", files, "Benchmark")

            migration_types = [
                "dcim.site", "dcim.manufacturer", "dcim.devicerole",
                "tenancy.tenantgroup", "ipam.rir", "virtualization.clustertype",
            ]
            registry = load_registry()
            for mode, max_batch_size, synchronous_off in (
                ("bulk", 100, False), ("per-item", 0, False), ("bulk synchronous=OFF", 100, True),
            ):
                database = tempfile.NamedTemporaryFile(suffix=".db", delete=False); database.close()
                migration_engine = create_engine(
                    f"sqlite:///{database.name}", connect_args={"check_same_thread": False},
                )
                if synchronous_off:
                    @event.listens_for(migration_engine, "connect")
                    def disable_sync(connection, _record):
                        connection.execute("PRAGMA synchronous=OFF")
                install_db_instrumentation(migration_engine)
                Base.metadata.create_all(migration_engine)
                MigrationSession = sessionmaker(bind=migration_engine)
                metrics = {
                    "commit_count": 0,
                    "bytes_fsynced": None,
                    "objects": args.migration_scale,
                    "max_batch_size": max_batch_size,
                }
                event.listen(MigrationSession, "after_commit", lambda _session: metrics.__setitem__(
                    "commit_count", metrics["commit_count"] + 1,
                ))
                migration_db = MigrationSession()
                try:
                    source_instance = models.NetboxInstance(
                        name=f"Migration source {mode}", base_url=netboxes[0].base_url,
                        api_token_encrypted=crypto.encrypt("token"), verify_ssl=False,
                    )
                    target_instance = models.NetboxInstance(
                        name=f"Migration target {mode}", base_url=netboxes[1].base_url,
                        api_token_encrypted=crypto.encrypt("token"), verify_ssl=False,
                    )
                    migration_db.add_all([source_instance, target_instance]); migration_db.flush()
                    job = models.MigrationJob(
                        source_instance_id=source_instance.id, target_instance_id=target_instance.id,
                        status="planned", selected_types_json=json.dumps(migration_types),
                        resolved_types_json=json.dumps(migration_types), options_json="{}",
                    )
                    migration_db.add(job); migration_db.flush()
                    for index in range(args.migration_scale):
                        type_index = min(
                            len(migration_types) - 1,
                            index * len(migration_types) // args.migration_scale,
                        )
                        object_type = migration_types[type_index]
                        migration_db.add(models.MigrationJobItem(
                            job_id=job.id, order_index=index, object_type=object_type, source_id=index + 1,
                            source_natural_key=f"bench-{index}", planned_action="create",
                            payload_json=json.dumps({"name": f"Bench {index}", "slug": f"bench-{index}"}),
                            fk_refs_json="{}", deferred_fk_json="{}", dropped_custom_fields_json="[]",
                        ))
                    migration_db.commit()
                    client = RateLimitedClient(
                        netboxes[1].base_url,
                        netbox_client.get_client(netboxes[1].base_url, "token", False),
                        max_requests_per_second=args.migration_rps,
                    )
                    with measured(
                        f"6 migration {mode}: {args.migration_scale} objects/6 types",
                        netboxes=netboxes[:2], extra_metrics=metrics,
                    ):
                        execute_job(
                            migration_db, job, registry=registry, target_client=client,
                            max_batch_size=max_batch_size,
                        )
                finally:
                    migration_db.close(); migration_engine.dispose(); Path(database.name).unlink(missing_ok=True)

            for size_mib in (100, 40):
                archive = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                archive.close()
                try:
                    with ZipFile(archive.name, "w", compression=ZIP_STORED) as output:
                        with output.open("root/device-types/Vendor/padding.bin", "w") as member:
                            chunk = b"x" * 1048576
                            for _ in range(size_mib): member.write(chunk)
                        output.writestr("root/device-types/Vendor/switch.yml",
                                        "manufacturer: Vendor\nmodel: Switch\nslug: switch\n")
                    github_repo._metadata_cache.clear()
                    written = {"bytes_written_to_temp": 0, "archive_result": "success"}
                    original_spool = github_repo.tempfile.SpooledTemporaryFile

                    class CountingSpool:
                        def __init__(self): self.file = original_spool(max_size=github_repo._ARCHIVE_SPOOL_BYTES)
                        def __enter__(self): self.file.__enter__(); return self
                        def __exit__(self, *values): return self.file.__exit__(*values)
                        def write(self, data):
                            count = self.file.write(data)
                            written["bytes_written_to_temp"] += count
                            return count
                        def __getattr__(self, name): return getattr(self.file, name)

                    with measured(f"7 archive scan: {size_mib} MiB", extra_metrics=written):
                        with patch.object(github_repo.requests, "get", return_value=StreamedFileResponse(archive.name)), \
                             patch.object(github_repo.tempfile, "SpooledTemporaryFile", return_value=CountingSpool()):
                            try:
                                github_repo.scan_device_type_metadata(
                                    f"archive-pat-{size_mib}", "owner/repo", "main", "device-types",
                                )
                            except ValueError:
                                written["archive_result"] = "rejected"
                finally:
                    Path(archive.name).unlink(missing_ok=True)
    finally:
        for instance in netboxes: instance.close()
    return _current_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--latency", type=float, default=0.03)
    parser.add_argument("--repo-size", type=int, default=2000)
    parser.add_argument("--migration-rps", type=float, default=4.0)
    parser.add_argument("--migration-scale", type=int, default=200)
    args = parser.parse_args()
    results = run(args)
    output = Path(__file__).parent / "results" / f"{args.label}.json"; output.parent.mkdir(exist_ok=True)
    document = {"label": args.label, "fake_latency_seconds": args.latency,
                "repo_size": args.repo_size, "migration_rps": args.migration_rps,
                "migration_scale": args.migration_scale, "results": results}
    output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"{'scenario':46} {'wall':>8} {'github':>8} {'netbox':>8} {'db':>6} {'peak MiB':>10}")
    for row in results:
        print(f"{row['scenario'][:46]:46} {row['wall_seconds']:8.2f} {row['github_calls']:8} "
              f"{row['netbox_calls']:8} {row['db_queries']:6} {row['peak_tracemalloc_mib']:10.2f}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
