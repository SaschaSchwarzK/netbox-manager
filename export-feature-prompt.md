# Task: Add a "Data Export" feature to NetBox Manager

Implement the feature below in the stages given. Everything you need to know about the codebase is in the "Codebase facts" section, so do NOT do a general exploration or write a plan. Only read the files each stage tells you to read. Reuse existing code and patterns; do not introduce a new framework, auth mechanism, logging setup, task queue or ORM.

Rules:
- Do NOT make git commits.
- Do not ask me questions. Where something is unclear, apply the decision in this prompt, or the option closest to existing code, and list it under "Decisions" in your final summary.
- Make minimal edits to existing files; put new code in new modules.
- After each stage run the checks (commands under "Checks") and fix failures before moving on.
- Write the tests of a stage in that stage, in the existing style (see "Test conventions").

## Feature summary

A new page "Data Export" (route `/export`) with two tabs:

1. **New export**: select a NetBox instance, a tenant, one or more object types (Devices, Virtual Device Contexts, Virtual Machines), the optional fields per type (including ALL custom fields of that NetBox instance, rendered dynamically), the format (CSV or Excel), then start a background job.
2. **Jobs**: the current user's export jobs (status, progress, created, finished, **expires at**) with download / cancel / delete actions.

Fixed requirements:
- Always exported: `name`, `primary_ip` (address of the API field `primary_ip`, empty if unset), `serial`. Only Devices have `serial` in NetBox. Export the `serial` column for Devices only; VDCs and VMs get no serial column.
- Optional standard fields (unchecked by default):
  - Device: `site`, `region`, `device_type`, `manufacturer`, `virtual_chassis`
  - Virtual Device Context: `primary_device` (API field `device`; UI label "Primary device")
  - Virtual Machine: `site`, `region`, `cluster`
- Optional custom fields: all custom fields of the instance that apply to that object type, fetched at runtime, never hardcoded.
- Formats: CSV (UTF-8 with BOM, delimiter `,` or `;`, default `,`) or XLSX. One job may contain several object types: XLSX = one worksheet per type ("Devices", "VirtualDeviceContexts", "VirtualMachines"); CSV = a single CSV for one type, a ZIP with one CSV per type for several.
- Only the user who started a job can see and download it. No admin override. Others get 404.
- Files are deleted 7 days after job completion; `expires_at` is shown in the job list.
- Files live on a NEW read-write volume (the container root filesystem is read-only).

## Codebase facts (already verified, do not re-discover)

Backend (`backend/app`, Python 3.12/3.14, FastAPI, SQLAlchemy 2 + SQLite in WAL mode, Alembic, APScheduler):
- Single process: `deploy/serve.py` starts ONE uvicorn process (no workers) plus Caddy. The app is documented as one container with no task queue. So no cross-process locking is needed.
- Config: `config.py`, pydantic-settings `Settings`, env prefix `NBM_`. Test `backend/tests/test_env_example.py` fails unless EVERY new `NBM_*` setting is documented in `backend/.env.example` (a line `NBM_X=value` or `#NBM_X=value`).
- Auth: session cookie -> `auth.get_current_user_optional(request)` returns a dict with `sub`, `name`, `email`, `username`, `groups`, `local`, or None. `auth.get_current_actor(request)` returns `{sub, name, email, username}` and an "anonymous (auth disabled)" actor with `sub=None` when `AUTHENTICATION_DISABLED=True`. `main.AuthRequiredMiddleware` already returns 401 for every `/api/*` request without a session (except `/api/auth`, `/api/health`).
- Authorization: `rbac.py`. Roles viewer < editor < admin. Use `Depends(require_role("viewer"))` which returns an `AccessContext`. Instance visibility: `rbac.require_visible("instance", instance_id, ctx, db)` (raises 404). Local admin and auth-disabled mode resolve to admin.
- NetBox instances: `models.NetboxInstance` (`id`, `name`, `base_url`, `api_token_encrypted`, `verify_ssl`, `ca_bundle_pem`). Build a client exactly like `routers/migrations.py::list_instance_tenants` does:
  `build_client(instance.base_url, crypto.decrypt(instance.api_token_encrypted), netbox_client.verify_for_instance(instance), read_only=True, max_requests_per_second=...)` from `app.services.migration.client`. This returns a `RateLimitedClient` with `.nb` (pynetbox), `.call(fn)` (throttle + retry/backoff on 429/5xx/connection errors) and `.paginated(endpoint, **filters)` which yields raw dict rows page by page with per-page retry (default `limit=200`, overridable by passing `limit=`). Tokens are stored encrypted in the app DB, so a background job simply re-reads the instance row and decrypts the token itself; nothing sensitive needs to be passed around or stored in the job.
- Tenants: the endpoint `GET /api/migrations/instances/{instance_id}/tenants?q=` already exists (viewer role, visibility checked, fully paginated) and the frontend has `migrationsApi.tenants(instanceId, q)`. REUSE it from the new page; do not write a second tenants endpoint.
- Custom field helpers: `services/netbox_customfields.py` has `_object_type_strings()`, `_content_type_string()`; `services/netbox_client.py` has `require_custom_fields_version`, `_parse_netbox_version`, `get_client`, `verify_for_instance`. A fixture with the NetBox 4.7.2 nested field shapes exists at `backend/tests/fixtures/netbox_openapi_nested_fields_4_7_2.json`.
- Background jobs: migration jobs run in daemon threads (`routers/migrations.py::start_job_worker`), use a DB row as the job record, a `cancel_requested` column polled by the worker, and a conditional UPDATE (`claim_job`) to avoid races. The worker uses its own `SessionLocal()` session. Follow these patterns.
- Scheduler: `main.start_scheduler()` builds an APScheduler `BackgroundScheduler(timezone="UTC")` and adds interval jobs; `main.lifespan` runs `run_schema_migrations()` then `start_scheduler()` then `resume_orphaned_migration_jobs()`, and shuts the scheduler down at exit.
- DB / migrations: `models.py` (Mapped/mapped_column style, `gen_uuid`, `utcnow` from `timeutil` = naive UTC datetime). Alembic head is `0004_reference_data_path`; `tests/test_database_schema.py` asserts the head revision string and MUST be updated when you add `0005`. `timeutil.as_utc_aware()` exists for serializing.
- Audit: `main.MutationAuditMiddleware` automatically writes a `models.AuditEvent` for every POST/PUT/PATCH/DELETE under `/api/` (action `<method>_<category>`, category = first path segment after `api`; `resource_id` = the first later path segment not in a skip set containing e.g. `cancel`, `execute`). Inserting an `AuditEvent` automatically forwards to syslog (event listener in `services/syslog_client.py`). Non-mutating events (e.g. downloads) must be written explicitly. Completion events of background jobs are sent with `services.syslog_client.send_audit_entry({...})` (see `services/migration/executor.py::_send_completion_audit` for the dict shape).
- Logging: `logger = logging.getLogger(__name__)` in every module; `main.configure_logging()` adds the request-id filter and optional JSON formatter. Never log tokens.
- Dependencies: `backend/requirements.txt` plus a hash-pinned `backend/requirements.lock` (pip-compile `--generate-hashes`; the Dockerfile installs with `--require-hashes`). `openpyxl` is NOT present.
- Lint: `ruff check backend` (rules E9, F only).
- Schemas: pydantic models live in `backend/app/schemas.py`; add the new ones there following neighbouring style.

Frontend (`frontend`, React + TypeScript + Vite, react-router, vitest):
- Pages are lazy-loaded and routed in `src/App.tsx` (sidebar `NavLink` list + `<Route>`). Add "Data Export" right after "Data Migration".
- API access: `src/api/client.ts` (`request<T>()` helper, `ApiError`, `instancesApi`, `migrationsApi`). Add an `exportsApi` block in the same style (the "Data migration" block starts around line 888).
- UI patterns: `src/pages/MigrationPage.tsx` (instance dropdown, tenant picker with `tenantsBusy`/cache, object type selection with grouped checkboxes, `notice` state `{kind, text}`, job status colors via CSS variables `--success --danger --accent --muted --border`). `src/pages/CustomFieldsPage.tsx` / `BulkImportPage.tsx` show the tab pattern (`<div className="tabs"><button className="active">`). `src/hooks/usePolling.ts` is a visibility-aware polling hook (`enabled, pollKey, initialValue, poll, isTerminal, onValue`). Global styles are in `src/styles.css` (classes `card`, `toolbar`, `field-help`, `primary`, `tabs`, tables).
- Caddy sets CSP `default-src 'self'`, so no external scripts/styles/fonts.

Deployment:
- `Dockerfile` (runs as uid/gid 65532, `/app/data` and `/app/certs` are created and chowned in the builder stage, then copied with `COPY --from=app-builder --chown=65532:65532`). `docker-compose.yml` (named volumes `dbdata`, `certdata`, `read_only: true`, `tmpfs /tmp:size=64m`, `mem_limit: 256m`, `pids_limit: 128`). `run-docker.sh` (volume env overrides `DATA_VOLUME`, `CERT_VOLUME`). README quick start uses `docker volume create` + `docker run --read-only ...`. There is NO Kubernetes/Helm in the repo; do not create any.
- IMPORTANT memory/disk constraint: `/tmp` is a 64 MiB tmpfs that counts against the 256 MiB memory limit. All export temp files must therefore live on the export volume, never in `/tmp`.

## Design decisions (already made)

1. **Instance selection is required.** The app manages many NetBox instances, so the export is per instance: instance dropdown -> tenant -> types/fields. Check `require_visible("instance", ...)`.
2. **Permission:** `require_role("viewer")` plus instance visibility (consistent with the existing Search page, which already reads device data with viewer). Define the minimum role as a single module constant `EXPORT_MIN_ROLE = "viewer"` in `routers/exports.py` so it is trivial to raise. No new role is invented.
3. **Ownership:** `owner_sub` = `get_current_actor(request)["sub"]`; when auth is disabled (sub is None) use the literal `"anonymous"`. If auth is enabled and the sub is empty, reject with 403. All job endpoints filter by `owner_sub == current owner` and return 404 otherwise.
4. **Persistence:** SQLAlchemy model + Alembic migration `0005_export_jobs` (no JSON files).
5. **Job execution:** a bounded `ThreadPoolExecutor` (size `NBM_EXPORT_MAX_CONCURRENT_JOBS`) in a runner module, with the worker using its own `SessionLocal()`. Jobs beyond the pool size stay `queued`.
6. **Restart behavior:** exports are not resumed. On startup, `queued`/`running` jobs become `failed` with error "Interrupted by restart", and `EXPORT_DIR/tmp` is emptied.
7. **API route layout** (chosen so the existing audit middleware yields `resource_id = job id`):
   - `GET  /api/exports/instances/{instance_id}/schema`
   - `POST /api/exports`
   - `GET  /api/exports`
   - `GET  /api/exports/{job_id}`
   - `POST /api/exports/{job_id}/cancel`
   - `DELETE /api/exports/{job_id}`
   - `GET  /api/exports/{job_id}/download`
   Register static routes before dynamic ones. Validate `job_id` as a UUID; a malformed id returns 404.
8. Add `"exports": "export"` to the `resource_type` mapping dict in `MutationAuditMiddleware` in `main.py` (one-line change).

## Stage 1: Config, dependency, storage, model

1. `config.py` + `backend/.env.example` (same commit-less change, the env example test must pass). Add to `Settings`:
   - `export_dir: str = "/app/exports"`
   - `export_retention_days: int = 7`
   - `export_max_concurrent_jobs: int = 2`
   - `export_max_active_jobs_per_user: int = 3`
   - `export_max_requests_per_second: float = 8`
   - `export_max_rows_per_type: int = 1_000_000` (XLSX hard limit is 1,048,575 data rows per sheet; fail the job with a clear message beyond this limit)
   - `export_min_free_mib: int = 200` (refuse to create a job when free space on the export volume is lower)
2. Dependency: add `openpyxl` (pin a current stable version) to `backend/requirements.txt`, then update `backend/requirements.lock` WITH HASHES by running `cd backend && pip install pip-tools && pip-compile --generate-hashes --output-file=requirements.lock requirements.txt` (pip-compile keeps existing pins by default). Verify with `git diff --stat backend/requirements.lock` that essentially only `openpyxl` and `et-xmlfile` were added (no unrelated upgrades; if other pins changed, revert them by hand). If there is no network access, do NOT invent hashes: add it to `requirements.txt`, stop this item and report it prominently in the final summary.
3. New package `backend/app/services/export/` with `__init__.py` and `storage.py`:
   - `ensure_dirs()` creates `EXPORT_DIR`, `files/`, `tmp/`. If creation or a write test fails (volume missing or read-only), log an ERROR and mark the feature unavailable (module flag); the app must still start and all export endpoints return 503 "Export storage is not writable. Mount a writable volume at NBM_EXPORT_DIR." Do not crash the app.
   - `final_path(job_id, ext)` -> `EXPORT_DIR/files/<uuid>.<ext>`, `tmp_path(...)`. Only `csv|xlsx|zip` extensions, job ids must match the UUID regex, resolve and assert the result is inside `EXPORT_DIR` (reuse the idea of `path_safety.validate_segment` where it fits). Never put user input in a filesystem path.
   - `free_mib()` via `shutil.disk_usage`, `delete_job_files(job)`, `sweep_tmp(older_than=None)`.
4. `models.py`: add `ExportJob` (Mapped style, `__tablename__ = "export_jobs"`): `id` (String(36), pk, `gen_uuid`), `instance_id` (FK `netbox_instances.id`, `ondelete=SET NULL`, nullable), `instance_name` (snapshot), `owner_sub` (String(255), not null), `actor_name`, `actor_email`, `tenant_id` (int), `tenant_name`, `tenant_slug`, `object_types_json`, `fields_json` ({type: {"optional": [...], "custom_fields": [...]}}), `format` (csv|xlsx), `delimiter` (String(1)), `status` (queued|running|completed|failed|cancelled|expired), `cancel_requested` (bool), `progress_json` ({type: {"done": n, "total": n}}), `row_counts_json`, `error` (String(512), no stack traces/secrets), `created_at`, `started_at`, `finished_at`, `expires_at` (DateTime, naive UTC via `utcnow`), `file_name` (String(64)), `file_size` (BigInteger), `download_name` (String(255)). Indexes: `(owner_sub, created_at)`, `status`, `expires_at`.
5. `backend/alembic/versions/0005_export_jobs.py` in the style of the existing files (`down_revision = "0004_reference_data_path"`, create table only if absent, indexes created explicitly). Update `tests/test_database_schema.py` to expect `0005_export_jobs` and to include `export_jobs` in the table assertion.
6. `backend/tests/conftest.py`: set `os.environ["NBM_EXPORT_DIR"]` to a temp directory at import time next to the existing `NBM_DATABASE_PATH` line, so tests never touch `/app/exports`.
7. Tests: storage (dirs created, path traversal / bad ids rejected, extension whitelist, unwritable volume -> unavailable flag).

## Stage 2: Schema discovery service + endpoint

Read first: `services/netbox_customfields.py` (helpers `_object_type_strings`), `services/netbox_client.py` (`require_custom_fields_version`, `_parse_netbox_version`), the 4.7.2 fixture (grep for `virtualdevicecontext`, `device_type`, `manufacturer`).

1. `services/export/schema.py`:
   - `TYPES` registry: for `device`, `virtualdevicecontext`, `virtualmachine` define label, the NetBox content-type string (`dcim.device`, `dcim.virtualdevicecontext`, `virtualization.virtualmachine`), REST endpoint attribute path, fixed columns, optional columns (key, label, source), sheet name, CSV file name.
   - `fetch_schema(client)`: read all custom fields with `client.paginated(client.nb.extras.custom_fields, limit=1000)`, keep those whose `object_types` (NetBox >= 4.0) or fallback `content_types` contains the type's content-type string. Each entry: `name`, `label` (fallback name), `type` (value of the `type` choice object/string), `group_name`, `weight`, `choice_set` not needed. Sort by `group_name` (empty last), `weight`, `label`. Import `_object_type_strings` from `netbox_customfields` (rename to a public alias there if you prefer; do not copy it).
   - VDC availability: probe `dcim/virtual-device-contexts/?limit=1` through `client.call`; on 404 omit the type from the schema. Other errors propagate.
   - Cache the result per `instance_id` for 60 s (in-process dict + lock, max 32 entries).
2. Router skeleton `routers/exports.py` with prefix `/api/exports`, tag `exports`; implement `GET /instances/{instance_id}/schema` (`require_role(EXPORT_MIN_ROLE)`, `require_visible`, map NetBox errors to HTTP 502 with a short message, the same way `list_instance_tenants` does). Response: `{object_types: [{key, label, fixed: [{key,label}], optional: [{key,label}], custom_fields: [{name,label,type,group_name,weight}]}]}`. Include the router in `main.py` (`app.include_router(exports.router)` and the import) now so tests can use it.
3. Tests with `responses`: both `object_types` and `content_types` variants, sorting, VDC 404 omitted, caching (second call makes no HTTP request), instance visibility.

## Stage 3: Export engine (no FastAPI imports)

Create `services/export/engine.py` with `run_export(job_spec, client, progress, is_cancelled) -> ExportResult` (a plain dataclass/dict spec so it is unit-testable without the DB).

1. Minimal backward-compatible edit to `RateLimitedClient.paginated` in `services/migration/client.py`: add a keyword-only optional `on_page: Callable[[dict], None] | None = None` that is called with each decoded page payload (needed for `count`). Do not change behavior otherwise; existing migration tests must still pass.
2. Per selected type, call `client.paginated(endpoint, tenant_id=<id>, limit=500, ordering="id", fields="<only needed top-level fields>", on_page=...)`:
   - Endpoints: `client.nb.dcim.devices`, `client.nb.dcim.virtual_device_contexts`, `client.nb.virtualization.virtual_machines`.
   - `fields=` is supported from NetBox 4.0; if `require_custom_fields_version` already enforces >= 4.0 just use it, otherwise skip the parameter below 4.0.
   - Needed fields: always `id,name,primary_ip,custom_fields`; Device: `serial` plus the selected ones among `site,device_type,virtual_chassis`; VDC: `device`; VM: `site,cluster`. Include only what is needed.
   - Total from `count` of the first page; report progress (`done`, `total`) after every page.
3. Value resolution (no per-object extra calls):
   - Nested objects: `name`, else `display`, else empty. `primary_ip`: `primary_ip.address` or empty. `serial`: string or empty.
   - `manufacturer`: `device_type.manufacturer.name` from the nested brief. Verify in the 4.7.2 fixture that the nested device-type brief includes `manufacturer`; if not, load `dcim/device-types/` once (`fields=id,manufacturer`) into an id->manufacturer-name dict and use that.
   - `region`: not on the object. When selected, load all sites once (`dcim/sites/`, `fields=id,region`, paginated) into `site_id -> region name` (direct region only, document that decision) and look up via the object's `site.id`.
   - Custom fields by type: text/longtext/integer/decimal/date/datetime/url/selection -> string; boolean -> `true`/`false` (empty when null); select -> the `label` if the API returns an object else the value; multiselect -> labels/values joined with `; `; object -> `display`/`name`; multiobject -> names joined with `; `; json -> compact JSON; None -> empty string. Unknown types fall back to `str()` / compact JSON for dict/list.
   - Column headers: standard columns use the plain keys (`name`, `primary_ip`, `serial`, `site`, `region`, `device_type`, `manufacturer`, `virtual_chassis`, `primary_device`, `cluster`); custom fields use `cf_<name>`. Order: fixed, optional (schema order), custom fields (schema order).
4. Spreadsheet-injection protection, applied to every string cell in CSV and XLSX: if the first character is one of `= + - @ \t \r`, prefix a single quote `'`. Exception: strings that fully match a plain number (`^[+-]?\d+(\.\d+)?$`) are left unchanged.
5. XLSX writer (`openpyxl`, `Workbook(write_only=True)`): one sheet per type, bold header row via `WriteOnlyCell`, `ws.freeze_panes = "A2"` set before appending rows, all values written as strings, strip characters rejected by openpyxl (`openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE`), truncate cells to 32,767 characters.
   **Critical:** openpyxl's write-only mode buffers sheet XML in temp files created via `tempfile` (default `/tmp` = 64 MiB tmpfs counted against the 256 MiB memory limit). Read the installed openpyxl source to confirm, then make those temp files go to `EXPORT_DIR/tmp`. Because `tempfile.tempdir` is global, do it through one small context manager that holds a module-level lock and restores the previous value, and document the trade-off in a comment. Add a test proving that no openpyxl temp file is created outside `EXPORT_DIR/tmp`.
6. CSV writer: standard `csv` module, `encoding="utf-8-sig"`, `newline=""`, delimiter from the job, streamed row by row to `EXPORT_DIR/tmp/<job>-<type>.csv`. Multi-type CSV: write each CSV, then `zipfile.ZipFile(..., ZIP_DEFLATED)` into tmp. Never hold all rows in memory.
7. Finishing: `os.replace` the result from `tmp/` into `files/<job_uuid>.<ext>` (same volume, so atomic). Always remove tmp files in a `finally`.
8. Cancel: check `is_cancelled()` after each page; on cancel raise an internal `ExportCancelled`, clean up, return. Row limit: exceeding `export_max_rows_per_type` fails the job with message "More than N rows for <type>; narrow the export." Failures from NetBox (the client already retries) become a short sanitized error message (no URLs with tokens, no stack trace).
9. Tests (`responses` + tmp dir): serialization for every custom field type, region and manufacturer resolution (and the manufacturer fallback), injection prefixing incl. the numeric exception, CSV BOM + both delimiters, XLSX readable by `openpyxl.load_workbook(read_only=True)` with correct sheets/headers/frozen row, multi-type ZIP, pagination across 2+ pages with a mocked `next` link and `count`, cancel path leaves no tmp files, row-limit failure, NetBox 500 failure path.

## Stage 4: Runner, lifecycle, cleanup

1. `services/export/runner.py`:
   - Module-level `ThreadPoolExecutor(max_workers=settings.export_max_concurrent_jobs, thread_name_prefix="export")`, `submit_job(job_id)`, `shutdown()`.
   - Worker `_run(job_id)` with its own `SessionLocal()`: atomically claim `queued -> running` with a conditional UPDATE (same pattern as `routers/migrations.claim_job`); if the claim fails (cancelled meanwhile) just return. Load the instance row; if it is gone, fail with "Instance no longer exists". Build the client from the stored encrypted token (see facts). Run `engine.run_export`. Progress callback writes `progress_json` at most every 2 s (separate short transactions); `is_cancelled` re-reads `cancel_requested` from the DB at most every 2 s.
   - On success: `status=completed`, `finished_at=utcnow()`, `expires_at=finished_at + retention`, `file_name`, `file_size`, `row_counts_json`. On cancel: `cancelled`, `finished_at`, `expires_at`. On any other exception: `failed`, short sanitized `error`, `logger.exception(...)` with the full trace, tmp cleaned. ALL terminal states set `expires_at = finished_at + retention` so the cleanup also removes failed/cancelled rows.
   - After the terminal commit call `send_audit_entry({"action_type": "export", "target_name": "<instance> / <tenant>", "file_path": job.id, "status": "success"|"error"|"cancelled", "detail": "...row counts, no data...", "actor_name": ..., "actor_email": ...})`.
2. `services/export/cleanup.py`:
   - `recover_after_restart()`: mark `queued`/`running` as `failed` ("Interrupted by restart"), set `finished_at`/`expires_at`, empty `tmp/`.
   - `run_cleanup()`: (a) for jobs with `expires_at < now` in a finished state: delete the file, set `status="expired"`, `file_name=None`; (b) delete job rows whose `expires_at < now - 24h`; (c) delete files in `files/` that have no job row AND are older than 1 hour (the file is moved into place just before the DB commit); (d) delete tmp files older than 24 h. Log one INFO line with the counts.
3. `main.py` (small edits): in `lifespan` after `run_schema_migrations()` call `storage.ensure_dirs()` and `cleanup.recover_after_restart()`; in `start_scheduler()` add `scheduler.add_job(cleanup.run_cleanup, "interval", minutes=15, id="export_cleanup", replace_existing=True)` and also run it once at startup; in the `finally` of `lifespan` call `runner.shutdown()` (`wait=False, cancel_futures=True`).
4. Tests: lifecycle queued -> running -> completed against a mocked NetBox (use a file DB like `tests/test_migration_router_integration.py` does for threaded tests), failure path, cancel while running, cancel while queued (the conditional claim), restart recovery, cleanup (expired deleted + status updated, non-expired untouched, young orphan kept, old orphan removed, row deletion after 24 h).

## Stage 5: Endpoints, authorization, audit, logging

In `routers/exports.py` (add pydantic models to `schemas.py`; follow how `MigrationJobSummary` is built and serialize datetimes as UTC using `timeutil.as_utc_aware` so the browser does not misread naive timestamps; check how other schemas/pages already handle this and stay consistent):

1. `POST /api/exports` (role viewer + `require_visible("instance")`; actor via `get_current_actor`). Body: `instance_id`, `tenant_id`, `object_types` (non-empty, subset of schema), `fields` per type (`optional: [keys]`, `custom_fields: [names]`), `format` (csv|xlsx), `delimiter` (`,`|`;`, default `,`, only meaningful for csv). Server-side validation against the live (cached) schema: unknown type/optional key/custom field -> 400 naming the offender; empty object_types -> 400; tenant must exist (`client.get(client.nb.tenancy.tenants, id=...)`, store name+slug) else 400; per-user active (queued/running) job limit -> 429 with a clear message; free-space check -> 507-style 503 message; storage unavailable -> 503. Create the row (`queued`), `download_name = export_<instance>_<tenantslug>_<YYYYmmdd-HHMMSS>.<ext>` sanitized to `[A-Za-z0-9._-]`, submit to the runner, return the job summary.
2. `GET /api/exports` (own jobs, newest first, limit 200), `GET /api/exports/{job_id}`.
3. `POST /api/exports/{job_id}/cancel`: only `queued|running`: conditional UPDATE (`queued -> cancelled` immediately with finished/expires set; `running -> cancel_requested=True`). Other states -> 409.
4. `DELETE /api/exports/{job_id}`: only finished states (`completed|failed|cancelled|expired`), delete file + row, 204.
5. `GET /api/exports/{job_id}/download`: owner match AND `status == "completed"` AND `expires_at > now` AND file exists, else 404 (410 is NOT used; never reveal other users' jobs). Resolve the path only through `storage.final_path(job.id, ext)`. Return `FileResponse(path, media_type=..., filename=job.download_name)` (`text/csv`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `application/zip`) so large files are streamed from disk. Add a `models.AuditEvent` (action `download_exports`, resource_type `export`, resource_id job id, status success) in the same request; the existing listener forwards it to syslog, so do NOT call `send_audit_entry` again for downloads.
6. Every job endpoint filters by `owner_sub`; log access to a job id that exists but belongs to someone else at WARNING (still return 404).
7. Logging (module logger, INFO unless noted): job created (actor sub, job id, instance id, tenant id, types, format), started, completed (row counts, duration, file size), failed (ERROR + trace, in the runner), cancelled, downloaded (actor, job id), deleted, cleanup counts. Never log tokens, row data or custom-field values.
8. Add `"exports": "export"` to the `resource_type` dict in `MutationAuditMiddleware` (Decision 8).
9. Tests (function-level style, `responses` mocks): auth/instance visibility (instance scoped to another group -> 404), owner isolation (user B gets 404 for GET/cancel/delete/download of user A's job), validation errors (unknown optional field, unknown custom field, no object type, unknown tenant), per-user limit, download of expired job -> 404 even if the file still exists, download of non-completed job -> 404, `download` audit event written, auth-disabled mode uses owner `anonymous`, 503 when storage unavailable.

## Stage 6: Frontend

Read first: `MigrationPage.tsx` (instance dropdown, tenant picker, selection UI, `notice`, status colors), one page with `tabs` (`CustomFieldsPage.tsx`), `usePolling.ts`, `styles.css`, `src/test/setup.ts` and `ReferenceDataPage.test.tsx` (test style).

1. `src/api/client.ts`: types `ExportSchemaType`, `ExportJob`, `ExportCreateRequest` and `exportsApi` = `schema(instanceId)`, `create(body)`, `list()`, `get(id)`, `cancel(id)`, `remove(id)`, `downloadUrl(id)` (returns `/api/exports/${id}/download`).
2. `src/pages/ExportPage.tsx` (default export), lazy-loaded in `App.tsx`, route `/export`, sidebar link "Data Export" after "Data Migration".
3. **Tab "New export"**
   - Instance dropdown (`instancesApi.list()`), tenant picker populated through `migrationsApi.tenants(instanceId)` with type-to-search; copy the loading/cache behavior from MigrationPage (extract a tiny shared component only if it is trivial; otherwise duplicate minimally).
   - On instance change load `exportsApi.schema(instanceId)`; show loading state and an inline error if it fails. Reset the type/field selection when the instance changes.
   - Object type checkboxes built from the schema (so a missing VDC type is simply not shown).
   - For each checked type a card rendered entirely from the schema: fixed fields shown checked + disabled; optional standard fields as checkboxes (unchecked); a "Custom fields" group with a checkbox per custom field (label plus small type hint), grouped by `group_name` when present, "select all / none" buttons, and a filter input when there are more than 10 custom fields. No field name or custom field is hardcoded in the TSX.
   - Format radio CSV / Excel; delimiter select (Comma, Semicolon) only for CSV.
   - "Start export" disabled until instance, tenant and at least one type are chosen and while busy. On success show a success notice, refresh the job list and switch to the Jobs tab. Show server validation errors (400/429/503 messages) inline using `ApiError.message`.
4. **Tab "Jobs"**
   - Table columns: Created, Instance, Tenant, Objects, Format, Status (colored badge), Progress (bar from the sum of `done/total` while running, otherwise the total row count), Finished, **Expires at**, Actions.
   - Actions: Download (an `<a href={exportsApi.downloadUrl(id)}>` styled as a button, only when `completed`), Cancel (queued/running), Delete (finished states, with `window.confirm`). Expired jobs show an "Expired" badge and no download.
   - Refresh via `usePolling` (list endpoint): once on tab open, then while any job is queued/running; stops otherwise; restart polling after a new job is created (change `pollKey`).
   - Timestamps formatted in browser local time with the UTC value in the `title` tooltip. Empty state text when there are no jobs.
5. Escape-safe rendering only (plain JSX text, never `dangerouslySetInnerHTML`) for tenant names, custom field labels and error text.
6. Tests (vitest): the page renders fixed/optional/custom-field checkboxes purely from a mocked schema (add a custom field in the mock and assert it appears), start button enable/disable rules, delimiter select only for CSV, Jobs table shows the expires column, download link only for `completed`, no download link for `expired`.

## Stage 7: Deployment, docs, final verification

1. `Dockerfile`: in the builder stage create `/app/exports` together with `/app/data /app/certs` (same `mkdir -p` + `chown -R 65532:65532 /app` line) and add `COPY --from=app-builder --chown=65532:65532 /app/exports /app/exports` in the runner stage (this is what gives a fresh named volume the right ownership). Do not change anything else.
2. `docker-compose.yml`: add `exportdata:/app/exports` to the service volumes and `exportdata:` to the top-level volumes; keep `read_only: true`. Add a short comment that export temp files are written to this volume, not to `/tmp`.
3. `run-docker.sh`: add `EXPORT_VOLUME` (default `netbox-manager_exportdata`) to the usage text, volume creation and the `docker run` mount `/app/exports`, following exactly how `DATA_VOLUME`/`CERT_VOLUME` are handled.
4. README quick start: add `docker volume create netbox-manager_exportdata` and `--volume netbox-manager_exportdata:/app/exports` to the `docker run` example; add a short "Data export" feature note. Also add the volume wherever else the repo documents volume lists (grep `certdata` and `CERT_VOLUME` across `README.md`, `docs/`, `scripts/container-smoke-test.sh`, `.github/workflows/*.yml`) and update the smoke test script/workflows if they start the container and would otherwise lack the writable volume.
5. `docs/user-guide.md`: a "Data Export" section after "Data Migration" (workflow, formats, injection protection note, retention, who can download). `docs/internal/api-routers.md`: add an "Exports" router section in the existing table format. Document the new settings in `backend/.env.example` with comments (already required by Stage 1) and in the README/docs where other `NBM_*` settings are described. Mention: single-instance deployment, the volume needs roughly the size of the largest expected exports times the number of exports within 7 days, bind mounts must be writable by uid 65532, and when authentication is disabled all users share the owner `anonymous`.
6. Final verification: run all checks; additionally run a manual smoke test against the fake NetBox in `backend/bench/fake_netbox.py` if it can serve devices/VMs cheaply (optional, skip if it would take more than a few minutes); run `docker build` only if Docker is available (skip otherwise and say so).

## Checks (run after every stage)

Backend (same as CI):
```
cd <repo root>
export AUTHENTICATION_DISABLED=True PYTHONPATH=$PWD/backend
export NBM_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
ruff check backend
python -m pytest -q backend/tests
```
(If `/app/data` is required by existing tests, create it as the CI workflow does: `sudo install -d -o "$(id -u)" -g "$(id -g)" /app/data`.)

Frontend (Stage 6 and 7):
```
cd frontend && npx tsc -b --noEmit && npm run build && npm test
```

## Test conventions (existing)
Tests call endpoint functions and services directly (no TestClient) with `responses` for NetBox HTTP; DB fixtures use a temp file SQLite with `models.Base.metadata.create_all` for threaded tests (see `tests/test_migration_router_integration.py`); the global test DB is isolated by `tests/conftest.py`. Keep tests fast; no `time.sleep` longer than needed, poll with a timeout.

## Final deliverable
A concise summary: files added/changed, new `NBM_EXPORT_*` settings and defaults, the new volume (`/app/exports`) in Dockerfile/compose/run script/README, the permission decision (viewer + instance visibility), the "Decisions" list, test/lint results, anything skipped (e.g. lock-file hashes without network, Docker build) and known limitations (single process, exports not resumed after restart, direct region only, anonymous shared owner when auth is disabled). Do not commit anything.
