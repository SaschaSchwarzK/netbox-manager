from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).with_name("results")
OUTPUT = ROOT / "docs" / "internal" / "performance.md"


def load(label: str) -> dict:
    return json.loads((RESULTS / f"{label}.json").read_text())


def row(document: dict, prefix: str) -> dict:
    return next(item for item in document["results"] if item["scenario"].startswith(prefix))


def metric(item: dict, name: str, suffix: str = "") -> str:
    value = item.get(name)
    return "not measurable" if value is None else f"{value}{suffix}"


def main() -> None:
    baseline = load("baseline")
    pre_remaining = load("after-9")
    archive = load("archive-cap")
    final = load("after-remaining")
    old_drift = row(pre_remaining, "1 scheduled drift")
    new_drift = row(final, "1 scheduled drift")
    old_archive = row(baseline, "7 archive scan: 100")
    rejected_archive = row(archive, "7 archive scan: 100")
    accepted_archive = row(archive, "7 archive scan: 40")
    bulk = row(final, "6 migration bulk:")
    per_item = row(final, "6 migration per-item:")
    sync_off = row(final, "6 migration bulk synchronous=OFF:")
    speedup = (per_item["wall_seconds"] - bulk["wall_seconds"]) / per_item["wall_seconds"] * 100
    sync_gain = (bulk["wall_seconds"] - sync_off["wall_seconds"]) / bulk["wall_seconds"] * 100

    text = f"""# Performance and reliability status

Generated from benchmark JSON by `python -m bench.render_docs`. Do not edit measured
tables by hand. The historical baseline used 30 ms fake-service latency and the old helper-only
migration scenario. The final run used {final['fake_latency_seconds']} s fake latency, a repository of
{final['repo_size']} definitions, {final['migration_scale']} migration objects, and the production
{final['migration_rps']} requests/second ceiling. Wall times from profiles with different inputs are
not compared; call and memory counts remain useful structural evidence.

## Work-item status

| Item | Status | Evidence / decision |
|---|---|---|
| 1. Archive spool | Done | 48 MiB configurable cap, free-space headroom, handled spool errors; 100 MiB rejected after {metric(rejected_archive, 'bytes_written_to_temp')} bytes and 40 MiB accepted. |
| 2. Session reads | Done | Touches are limited to once per 60 seconds per active session; request-scoped identity reuse and immediate expiry/revocation are tested. |
| 3. Drift calls | Partial | Markers are listed once per instance. Bulk component reads and changelog optimization are implemented but default off pending real-NetBox verification. |
| 4. Migration benchmark | Done | Real executor/database benchmark. Bulk was {speedup:.1f}% faster than per-item, so configurable batches remain; successful bulk results now commit together. AIMD was skipped. |
| 5. CI gates | Done | Locked backend install, Python-version matrix, Ruff, frontend tests, docs build, and scheduled dependency audit. |
| 6. Frontend tests | Done | API, polling, authenticated routing, and YAML round-trip tests run under Vitest/jsdom. |
| 7. Repository/docs hygiene | Done | Git-only source export, expanded container smoke test, consolidated env docs, generated performance tables. |
| 8. Legacy DB bridge | Done | Removed; fresh and Alembic-managed databases upgrade directly to head. |

## Item 1 — bounded archive input

| Profile | Result | Wall seconds | Bytes written to temp | Python peak | RSS peak |
|---|---:|---:|---:|---:|---:|
| Historical 100 MiB scan (128 MiB cap) | accepted | {old_archive['wall_seconds']} | not recorded | {old_archive['peak_tracemalloc_mib']} MiB | {old_archive['peak_rss_mib']} MiB |
| New 100 MiB scan (48 MiB cap) | {rejected_archive['archive_result']} | {rejected_archive['wall_seconds']} | {rejected_archive['bytes_written_to_temp']} | {rejected_archive['peak_tracemalloc_mib']} MiB | {rejected_archive['peak_rss_mib']} MiB |
| New 40 MiB scan | {accepted_archive['archive_result']} | {accepted_archive['wall_seconds']} | {accepted_archive['bytes_written_to_temp']} | {accepted_archive['peak_tracemalloc_mib']} MiB | {accepted_archive['peak_rss_mib']} MiB |

The cap applies independently to compressed download bytes and total uncompressed member bytes. The
8 MiB spool threshold is unchanged. `/tmp` remains a 64 MiB tmpfs and counts toward the container's
memory limit.

## Item 3 — scheduled drift

| Profile | Pairs | NetBox calls | Calls/pair | GitHub calls | DB statements | Wall seconds |
|---|---:|---:|---:|---:|---:|---:|
| Before batched markers | 600 | {old_drift['netbox_calls']} | {old_drift['netbox_calls'] / 600:.2f} | {old_drift['github_calls']} | {old_drift['db_queries']} | {old_drift['wall_seconds']} |
| Batched-marker first full pass | 600 | {new_drift['netbox_calls']} | {new_drift['netbox_calls'] / 600:.2f} | {new_drift['github_calls']} | {new_drift['db_queries']} | {new_drift['wall_seconds']} |

An unchanged non-full pass uses one paginated device-type marker listing per instance and no component
reads for pairs whose repository SHA and NetBox `last_updated` marker are unchanged. A full pass still
reads component templates. Component-only edits can therefore remain unseen until the configured full
recheck (roughly `NBM_DRIFT_FULL_RECHECK_EVERY ×` the scheduler interval) unless the optional changelog
marker is enabled and verified.

## Item 4 — real migration executor

| Mode | Objects | Batch size | Wall seconds | NetBox calls | DB statements | Commits | Bytes fsynced |
|---|---:|---:|---:|---:|---:|---:|---:|
| Bulk create | {bulk['objects']} | {bulk['max_batch_size']} | {bulk['wall_seconds']} | {bulk['netbox_calls']} | {bulk['db_queries']} | {bulk['commit_count']} | {metric(bulk, 'bytes_fsynced')} |
| Per-item create | {per_item['objects']} | {per_item['max_batch_size']} | {per_item['wall_seconds']} | {per_item['netbox_calls']} | {per_item['db_queries']} | {per_item['commit_count']} | {metric(per_item, 'bytes_fsynced')} |
| Bulk, SQLite synchronous=OFF | {sync_off['objects']} | {sync_off['max_batch_size']} | {sync_off['wall_seconds']} | {sync_off['netbox_calls']} | {sync_off['db_queries']} | {sync_off['commit_count']} | {metric(sync_off, 'bytes_fsynced')} |

Bulk create groups contiguous creates of the same object type into at most 100 records, now bounded by
the job's `max_batch_size`; zero forces per-item creates. Any failed bulk request falls back to its
individual records. The measured bulk improvement was {speedup:.1f}%, above the 30% retention rule.
Turning SQLite durability off changed wall time by {sync_gain:.1f}%, above the 10% decision threshold,
so successful remote bulk responses now persist their item ids in one local commit. Resume still checks
the target natural keys; a crash-window test proves that a remote success before the local batch commit
does not duplicate objects. AIMD was not added: the fixed rate is intentionally the configured hard
ceiling, so a controller constrained to that same ceiling cannot improve the steady-state default.

## Remaining hot spots (frequency × cost)

1. Full drift component reads: nine component endpoints per affected device type and instance.
2. Per-item migration fallback: required for attribution after a rejected bulk request, and deliberately
   rate-limited to the configured ceiling.
3. GitHub import content reads/writes: selected YAML and image files scale linearly after the tree scan.
4. Search/fleet fan-out: bounded, but still proportional to visible instances and searched endpoint types.

## Needs real-service verification

- NetBox multi-value `device_type_id` filtering on every component-template endpoint; guarded by
  `NBM_DRIFT_BULK_COMPONENT_READS=false`.
- NetBox 4.x `/api/core/object-changes/` content-type filters and installations with disabled or
  truncated change logging; guarded by `NBM_DRIFT_USE_CHANGELOG=false`. NetBox 3.x is outside the
  application's current minimum supported version.
- GitHub rate-limit behavior for the tree/blob and archive endpoints under real repository traffic.
"""
    OUTPUT.write_text(text)


if __name__ == "__main__":
    main()
