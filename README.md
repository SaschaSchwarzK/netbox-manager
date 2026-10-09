# NetBox Manager

> [!WARNING]
> **Development software — use at your own risk.** NetBox Manager is still under active
> development and may contain defects that cause unintended configuration changes or data loss.
> Create and verify current backups of every NetBox instance and other affected data before using
> the application, and ensure that you can restore those backups. The data-migration feature is
> still under testing and should be validated in a non-production environment before production use.

A web UI and Git-backed control plane for managing shared configuration, publishing changes,
searching infrastructure, migrating tenant data, and exporting inventory across multiple NetBox
instances. Device types, custom fields, reference data, and tenant permission templates live in
GitHub as reviewable YAML; NetBox Manager keeps connection settings, job state, and audit history
in a small local SQLite database.

For application workflows and annotated screenshot placeholders, see the
[NetBox Manager User Guide](docs/user-guide.md).
Contributor setup, tests, builds, internal documentation, and publishing are covered in the
[Development Guide](docs/internal/development.md).

## What NetBox Manager can do

NetBox Manager provides a Git-backed control plane for operating multiple NetBox instances from
one place.

| Use case | Capabilities |
|---|---|
| **Manage device types as code** | Create, import, and edit device-type YAML with structured forms, component grids, bulk-add patterns, custom fields, and a live YAML preview. Every save opens or updates a pull request, keeping GitHub as the source of truth. |
| **Publish safely across a fleet** | Preview per-instance differences before a push, select targets individually or by tag, require an approved and merged pull request, and detect configuration drift after deployment. |
| **Standardize shared NetBox data** | Manage custom fields, choice sets, manufacturers, tags, templates, device roles, platforms, webhooks, event rules, and config contexts in Git. Import existing state, review differences, and push changes without deleting objects. |
| **Find and assess infrastructure** | Search devices, virtual machines, virtual device contexts, IP addresses, prefixes, and MAC addresses across visible instances. Check instance health, NetBox versions, plugins, response times, token expiry, and device-type coverage. |
| **Automate tenant access** | Store tenant permission templates in Git, preview changes, onboard and decommission tenants, and apply reviewed permission updates across managed instances. |
| **Move and extract data** | Plan and run tenant-scoped migrations between NetBox instances with mapping review, retries, cancellation, and best-effort rollback. Export devices, virtual device contexts, and virtual machines to CSV or Excel with selectable standard and custom fields. |
| **Maintain accountability** | Use OIDC or a break-glass local administrator, scope roles to specific instances and repositories, retain an audit trail, attribute pull requests to the requesting user, and optionally forward audit events to syslog. |

The [User Guide](docs/user-guide.md) walks through each workflow in detail.

## Quick start

1. Pull the latest release from GitHub Container Registry:

   ```bash
   docker pull ghcr.io/saschaschwarzk/netbox-manager:latest
   ```

2. Download the environment template and generate the encryption key used for stored NetBox
   tokens and GitHub PATs:

   ```bash
   curl -fsSL \
     https://raw.githubusercontent.com/SaschaSchwarzK/netbox-manager/main/backend/.env.example \
     -o .env

   docker run --rm \
     --entrypoint /app/venv/bin/python \
     ghcr.io/saschaschwarzk/netbox-manager:latest \
     -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Copy `backend/.env.example` to `.env`, then put the generated value in `NBM_SECRET_KEY` there. Configure OIDC or the
   local administrator in the same file. For an explicitly open development instance, set
   `AUTHENTICATION_DISABLED=True`. Authentication remains enforced when this variable is false,
   malformed, or absent.

3. Create the persistent volumes and start the container:

   ```bash
   docker volume create netbox-manager_dbdata
   docker volume create netbox-manager_certdata
   docker volume create netbox-manager_exportdata

   docker run --detach \
     --name netbox-manager \
     --restart unless-stopped \
     --env-file ./.env \
     --publish 8443:8443 \
     --volume netbox-manager_dbdata:/app/data \
     --volume netbox-manager_certdata:/app/certs \
     --volume netbox-manager_exportdata:/app/exports \
     --read-only \
     --tmpfs /tmp:size=64m,mode=1777 \
     --cap-drop ALL \
     --security-opt no-new-privileges=true \
     --memory 256m \
     --cpus 1.0 \
     --pids-limit 128 \
     ghcr.io/saschaschwarzk/netbox-manager:latest
   ```

4. Open the UI at https://localhost:8443. API documentation is disabled by default; set
   `NBM_ENABLE_API_DOCS=true` to expose authenticated Swagger and OpenAPI endpoints. On first startup the application
   creates a self-signed certificate, so browsers will show a trust warning until you replace it
   with a certificate trusted by your environment.

The export volume is required by **Data Export** and must be writable by container UID 65532 when
using a bind mount. Size it for the largest expected exports multiplied by the number retained
during the configured retention period.

To update later, pull `latest` again and recreate the container while keeping all named volumes.
The `run-docker.sh` helper uses `./.env` when present, otherwise `backend/.env`; set `ENV_FILE`
explicitly to override that choice.

### Configuration and key rotation

[`backend/.env.example`](backend/.env.example) is the canonical list of environment variables.
Operational controls include `NBM_ENABLE_API_DOCS` (default `false`), `NBM_LOG_FORMAT` (`human` or
`json`), `NBM_AUDIT_RETENTION_DAYS` (default `365`, `0` keeps records forever),
`NBM_DATABASE_PATH` (default `/app/data/netbox_manager.db`),
`NBM_SESSION_MAX_AGE_HOURS`, `NBM_SESSION_IDLE_TIMEOUT_MINUTES`, and
`NBM_SESSION_TOUCH_INTERVAL_SECONDS` (default `60`; limits session activity writes),
`NBM_ARCHIVE_MAX_MIB` (default `48`, covering both compressed downloads and total uncompressed
YAML read from an archive), `NBM_ARCHIVE_LOCK_TIMEOUT_SECONDS` (default `120`; bounds waiting for
the single process-wide archive scanner),
`NBM_DRIFT_CHECK_INTERVAL_HOURS` (`0` disables scheduled drift checks),
`NBM_DRIFT_WORKERS`, `NBM_DRIFT_PER_INSTANCE_WORKERS`, `NBM_DRIFT_FULL_RECHECK_EVERY`,
`NBM_DRIFT_BULK_COMPONENT_READS` (experimental, default `false`), and
`NBM_DRIFT_USE_CHANGELOG` (experimental, default `false`),
`NBM_DRIFT_CHANGELOG_MARGIN_SECONDS` (default `300`; change-log clock-skew safety margin),
`NBM_SEARCH_LIMIT_PER_TYPE` and `NBM_SEARCH_TIMEOUT_SECONDS`,
`NBM_FLEET_CACHE_SECONDS` and `NBM_FLEET_TIMEOUT_SECONDS`,
`NBM_MIGRATION_AUTO_RESUME`, `NBM_MIGRATION_MAX_AUTO_RESUMES`, and the `NBM_EXPORT_*` controls
described in [`backend/.env.example`](backend/.env.example).

`NBM_SECRET_KEY` accepts a comma-separated Fernet key ring. To rotate it, prepend a new key, restart,
back up the database, and run this inside the container:

```bash
/app/venv/bin/python -m app.cli rotate-secrets
```

After verifying stored NetBox and GitHub credentials, remove the old keys and restart. The first key
always encrypts; the remaining keys are decrypt-only. NetBox instances can also store a private CA
bundle in the Instances editor, avoiding the need to disable TLS verification.

### SQLite backup and restore

Use SQLite's online backup command so the copy is consistent even while WAL mode is active:

```bash
docker exec netbox-manager sqlite3 /app/data/netbox_manager.db ".backup /tmp/backup.db"
docker cp netbox-manager:/tmp/backup.db ./netbox-manager-backup.db
```

Test restoration against a separate temporary container and volume before relying on the backup:

```bash
docker volume create netbox-manager_restore_test
docker run --rm -v netbox-manager_restore_test:/app/data -v "$PWD:/backup:ro" \
  --entrypoint sh ghcr.io/saschaschwarzk/netbox-manager:latest \
  -c 'cp /backup/netbox-manager-backup.db /app/data/netbox_manager.db && sqlite3 /app/data/netbox_manager.db "PRAGMA integrity_check"'
```

Expect `ok`. Restore production only while the application is stopped, by copying the verified file
to `/app/data/netbox_manager.db` in its persistent volume.

### Container runtime

The deployed container runs as UID/GID `65532`, drops all Linux capabilities, has a read-only root
filesystem, and writes only to the SQLite, certificate, and export volumes plus a small `/tmp`
tmpfs. The database defaults to `/app/data/netbox_manager.db` on the persistent data volume and can
be relocated with `NBM_DATABASE_PATH`; certificates are always
`/app/certs/cert.pem` and `/app/certs/key.pem`. The certificate paths cannot be overridden with
environment variables. The archive cap defaults to 48 MiB, leaving 8 MiB headroom in the default 64 MiB
`/tmp` tmpfs. A tmpfs consumes RAM and counts against the container's 256 MiB memory limit; the
application lowers the effective archive cap when less temporary space is available rather than
raising the tmpfs size.

To install a trusted certificate, replace `cert.pem` and `key.pem` in the certificate volume and
recreate the container. If either file is missing, the application generates a new self-signed
pair during startup.

## How device-types are stored

There is **no database table for device-types**. Instead:

- Under **GitHub Targets**, configure a repo + branch + path pattern (e.g. `myorg/devicetype-library`, `main`, `device-types/{manufacturer}/{slug}.yml`) and a PAT with repo access.
- The **Device Types** page lists every `.yml`/`.yaml` file under that path directly from GitHub (via the git trees API), and clicking one loads it straight from the repo into the editor.
- "New from scratch" and "Import YAML" commit a new file immediately.
- Saving in the editor is a commit — optionally routed through a PR instead of committing straight to the branch. GitHub's file SHA is used for optimistic concurrency, so if the file changed on GitHub since you loaded it, the save is rejected with a clear message rather than silently overwriting someone else's edit.
- The SQLite DB holds application state such as encrypted connection settings, job metadata, and
  audit/push history, but no device-type or reference-data content.

## Feature and operational details

The following sections describe important behavior, safeguards, and operational controls behind
the capabilities above.

### Data Export operations

A viewer with access to an instance can start an export. Jobs and downloads are private to their
creator; in authentication-disabled deployments all requests share the owner `anonymous`. Jobs run
in background threads in the single-process deployment and are not resumed after a restart.
Completed and failed jobs expire after seven days by default and can also be deleted from the UI.

`NBM_EXPORT_DIR`, `NBM_EXPORT_RETENTION_DAYS`, `NBM_EXPORT_MAX_CONCURRENT_JOBS`,
`NBM_EXPORT_MAX_ACTIVE_JOBS_PER_USER`, `NBM_EXPORT_MAX_REQUESTS_PER_SECOND`,
`NBM_EXPORT_MAX_ROWS_PER_TYPE`, and `NBM_EXPORT_MIN_FREE_MIB` control storage, retention, load, and
safety limits. The default directory is `/app/exports`; see the Quick start volume mount above.

### Change safety across many instances

- **Diff-before-push** — "Preview diff" in the Publish tab compares the GitHub source against what's actually deployed on each selected instance (base fields + per-component added/removed/changed), so a push's effect is visible before it happens.
- **Approval gate** — instances flagged "require an approved, merged PR" are checked at push time: the backend looks up the PR that produced the current file content and confirms it was merged *and* has at least one approving review. A push to such an instance is refused with a clear reason if that's not the case.
- **Bulk/grouped push** — instances can carry tags, and the Publish tab has "select by tag" buttons (e.g. click `region:eu` to select every instance with that tag) plus an "all" button, so pushing to a whole group is one click instead of checking boxes individually. The API also accepts a `tags` array directly for scripted/bulk pushes.
- **Drift detection** — a background job (interval set by `NBM_DRIFT_CHECK_INTERVAL_HOURS`, default 6; set to `0` to disable) periodically re-diffs every instance/device-type pair that's had a successful push in the past, flagging anything that's drifted from the GitHub source (hand-edited in NetBox's UI, partially failed push, etc.). Results are on the **Drift** page, with a "Check all now" button for an on-demand run.

Drift marker reuse avoids loading every component template on every pass. A component-only edit may
therefore remain undetected until the next forced full check, approximately
`NBM_DRIFT_FULL_RECHECK_EVERY × NBM_DRIFT_CHECK_INTERVAL_HOURS` hours. The experimental change-log
marker can shorten this window when enabled, but must be verified against the deployed NetBox version.
The latest drift-run counters and `degraded` indicator are available from the app-admin-only
`GET /api/admin/stats` endpoint; a run is degraded when list/bulk shortcuts fail or point-lookup
fallbacks exceed 10% of its pairs. Measured performance numbers are historical and live in
[`docs/internal/performance.md`](docs/internal/performance.md).

Before enabling either experimental drift shortcut, run the read-only compatibility checker against
a staging NetBox. It uses only GET requests, reads credentials from the environment, and never prints
the token:

```bash
NB_URL=https://netbox.example NB_TOKEN=... \
  python scripts/verify-netbox-compat.py --ca-bundle /path/to/netbox-ca.pem
```

Use `--insecure` only for an intentional staging test with unverified TLS. Component endpoints are
reported independently. For each endpoint, the checker examines the first 100 templates, selects up
to 10 represented parents from the first 200 device types, and compares one repeated-filter request
with the union of individual requests. A real pass requires templates on at least two distinct
parents, a non-empty matching union, and proof that a single-parent filter narrows the unfiltered
result. Missing evidence is `INCONCLUSIVE`, never a pass. The absent-id probe is informational because
NetBox versions may return either an empty `200` response or a `400` validation response. Failed
requests include the status, request path and query, and a redacted 200-character response snippet;
headers and the API token are never printed.

An accepted object-change filter does not prove that NetBox records edits under that content type.
On staging, edit one template of every supported component type and run the checker with a timestamp
from before the edits:

```bash
NB_URL=https://netbox.example NB_TOKEN=... \
  python scripts/verify-netbox-compat.py --since 2026-10-07T10:00:00Z \
  --staging-edit-verified
```

`--since` reports “N changes since &lt;time&gt;” for every content type and marks zero-count types as not
verified. `--staging-edit-verified` means: “I edited a template of every component type and confirmed
non-zero counts.” Without that explicit assertion, the checker always recommends
`NBM_DRIFT_USE_CHANGELOG=false`. For the end-to-end test, enable the flag on staging, run drift, edit
one component in NetBox, run drift again, and confirm that the pair is reported as changed on the next
pass. The checker recommends `NBM_DRIFT_BULK_COMPONENT_READS=true` only when every component endpoint
is a real pass; its false recommendation names every failed or inconclusive endpoint. Enable a
shortcut only when its recommendation is `true`. The checker deliberately performs no writes.

### Login (OIDC or break-glass local admin)

Authentication and authorization are enforced by default. Configure all three OIDC values
(`NBM_OIDC_ISSUER`, `NBM_OIDC_CLIENT_ID`, and `NBM_OIDC_CLIENT_SECRET`) or both local-admin
credentials. An incomplete method or no complete method makes the backend refuse to start.

`AUTHENTICATION_DISABLED=True` is the only way to disable authentication and authorization.
This variable deliberately has no `NBM_` prefix. When enabled, every request receives unrestricted
administrator access, so use it only for an intentionally open development deployment.

The local account is a break-glass administrator for identity-provider outages: it has app-admin
access and bypasses resource scoping. Use a long random password and keep
`NBM_SESSION_COOKIE_SECURE=true` when serving the application over HTTPS.

Once configured:
- `GET /api/auth/login` starts the standard OIDC authorization-code flow (via [Authlib](https://docs.authlib.org/)), redirecting to your provider.
- `GET /api/auth/callback` exchanges the code for tokens; Authlib validates the ID token's signature (against the provider's published JWKS), issuer, audience, expiry, and nonce as part of that exchange — this app doesn't hand-roll any of that verification.
- The browser receives only a random, HttpOnly session identifier. Its SHA-256 hash and the verified
  identity claims are stored server-side in SQLite. Sessions have configurable absolute and idle
  expiry, logout deletes the row, and administrators can revoke every session for a subject.
- Every `/api/*` route except `/api/auth/*` and `/api/health` requires a valid session unless authentication is explicitly disabled; unauthenticated requests get a plain 401.
- The frontend checks `/api/auth/me` on load: if not authenticated, it shows a sign-in screen instead of the app; once signed in, the sidebar shows the user's name and groups with a sign-out button.

Setting this up requires registering this app with your identity provider as a confidential web app/client, with `NBM_OIDC_REDIRECT_URI` as an allowed redirect URI. See `.env.example` for the full list of variables and provider-specific notes on the groups claim (Keycloak, Entra ID/Azure AD, and Okta all expose group membership differently).

### Fleet visibility

- **Instance health dashboard** (**Fleet** page) — for every NetBox instance, in parallel: reachable or not, NetBox version, installed plugin versions (all straight from `/api/status/`), and response latency. Useful before pushing a device type that needs a feature only newer NetBox versions support (module bays, virtual device contexts, etc.) — check here first rather than finding out from a failed push.
- **Token expiry warnings** — the same Fleet page checks GitHub PAT expiry (via the `github-authentication-token-expiration` response header GitHub sets on fine-grained and expiring classic PATs) and, best-effort, NetBox token expiry (via `/api/users/tokens/` — only reliable when a token can see exactly one token, itself; if multiple are visible we say so rather than guessing which one is active). Badges flag anything expired or expiring within 14 days.
- **Cross-instance coverage** — on the Device Types list, "Check coverage" per row answers "which instances have this device type, and are they up to date?" across *every* configured instance, not just ones it's been pushed to before (that's Drift) or a subset you've selected (that's the push-time diff preview).

### Custom-field values in device-type YAML

Device-type files use NetBox's native bulk-import columns for custom-field values. A custom field
named `eol_date` is written as a top-level `cf_eol_date` key, which keeps every repository file
directly importable through NetBox's device-type YAML import page:

```yaml
manufacturer: Cisco
model: C9300-24P
slug: c9300-24p
cf_eol_date: "2030-12-31"
cf_support_tier: gold
cf_replacement_cost: 4200
cf_requires_license: true
```

The field type is defined once in the separate custom-fields template and is not repeated in each
device-type file. Before manual import, that custom field must already exist in NetBox and include
`dcim.devicetype` in its `object_types`. Dates should use quoted ISO `YYYY-MM-DD` values; booleans
and numbers remain native YAML values; selection fields use their stored value rather than their
display label. Object custom fields typically contain a NetBox object ID and therefore are not
portable between instances.

NetBox Manager displays these values through its structured custom-field editor. At the boundaries
it converts `cf_<name>` YAML keys to the REST API's nested `custom_fields` object and back again.
Legacy manager files containing a nested `custom_fields` mapping remain readable and are converted
to NetBox-native `cf_*` keys when next saved.

- **Bulk import** (**Bulk Import** page) — imports from the public **NetBox Data Exchange
  (NDX)** catalog, a GitHub device-type library, or another NetBox instance. NDX search covers
  manufacturer, model, and part number and downloads the selected public YAML definitions. GitHub
  scans download one repository archive and parse searchable metadata without making one API call
  per file. All sources support filtering by manufacturer, model, and part number and land selected
  definitions in one shared branch and one pull request. Existing destination files are skipped.

## Troubleshooting

- **The UI loads but `/api/` returns 502** — Caddy is running but the Uvicorn child process failed. Run `docker compose logs app`; the most common cause is the secret-key issue below.
- **Backend crashes on startup with a Fernet/base64 error** — this means `NBM_SECRET_KEY` in `backend/.env` is still the placeholder value (or `backend/.env` was never created from `backend/.env.example` at all). The startup error now names this explicitly and tells you the exact command to generate a real key; see Quick start above.
- **Backend refuses to start because no authentication method is configured** — configure complete OIDC or local-admin credentials. For an intentionally open deployment, set `AUTHENTICATION_DISABLED=True` explicitly.

### Syslog forwarding

The audit log can be forwarded to a remote syslog server as structured **RFC 5424** messages — config-file only, on purpose: the **Syslog** page shows the current settings read-only plus a "Send test message" button, but changing where audit data goes isn't something the UI lets you do at runtime.

- `NBM_SYSLOG_ENABLED`, `NBM_SYSLOG_PROTOCOL` (`udp` or `tcp`), `NBM_SYSLOG_HOST`, `NBM_SYSLOG_PORT`, `NBM_SYSLOG_FACILITY` (standard syslog facility keyword, e.g. `local0`), `NBM_SYSLOG_APP_NAME` — see `.env.example`.
- Every audit entry (every GitHub save and NetBox push, success or failure) is forwarded automatically — this is wired as a SQLAlchemy `after_insert` event on the audit table itself, not called explicitly at each of the several places that write one, so a future audit-logging call site can't accidentally forget to forward.
- Message severity is informational for successful actions, warning for failed ones. Structured data carries actor name/email, action type, target, file path, and status as named fields (`[auditEntry@32473 actor="..." action="..." ...]`); the free-text detail becomes the MSG.
- A syslog delivery failure (unreachable host, connection refused) never breaks the actual audit-log write or the request that triggered it — it's caught and silently dropped for the automatic path, while the test-button path surfaces the real error so misconfiguration is visible when you're deliberately checking it.
- TCP framing is newline-delimited (RFC 6587 "non-transparent framing"), which is what most syslog receivers (rsyslog, syslog-ng) expect out of the box.

### Access control (roles & scoping)

The **Access Control** page manages one unified mapping table. Each row maps an OIDC group to a
role (`viewer`, `editor`, `admin`, or no role) and a scope. The `("*", "*")` scope means all
resources; a concrete scope names one NetBox instance or GitHub target. A null role is a
visibility-only grant. A resource with no concrete mappings remains visible to everyone, and a
global role does not reveal resources scoped to other groups.

A user's highest mapped role remains the coarse gate for unscoped actions, with `NBM_DEFAULT_ROLE`
as the fallback. Actions against a concrete instance or GitHub target additionally require the
necessary effective role on that resource: a global mapping applies everywhere, while a concrete
mapping only elevates that named resource. Thus an editor grant on instance A does not permit writes
to instance B. Resources without a concrete mapping retain the global/default role semantics.
`NBM_BOOTSTRAP_ADMIN_GROUPS` remains the permanent app-admin bootstrap. App admins manage mappings
and create/delete resources; a resource-scoped admin may edit that resource and now satisfies its
`requires_approved_pr` admin gate.

### Custom-fields template

A second GitHub-backed workflow, parallel to device-types: a single YAML file per repo (`custom_fields_path` on the GitHub target, default `custom-fields/template.yml`, in its own folder separate from device-types) listing every custom field and custom field choice set that should exist across the fleet.

NetBox Manager supports **NetBox 4.6.8 or newer**. Custom-field operations verify the version through
`/api/status/` and report a clear error for older or unverifiable instances. Template YAML uses the
`object_types` scope key from the supported NetBox API; pre-4.x `content_types` input is not supported.

- **Custom Fields** page — proper create/edit forms for both custom fields and choice sets, grouped into the same sections NetBox's own forms use (General / Values / Behavior / Validation Rules for fields; Name/Base Choices/Extra Choices/Order for choice sets), not a flat spreadsheet. Field coverage now matches NetBox's actual model, including several previously-missing attributes verified against NetBox's own source and docs: `unique`, `search_weight`, `comments`, `related_object_type` and `related_object_filter` (for object/multiobject fields), and `base_choices` (referencing a predefined choice set like IATA airport codes, ISO 3166 country codes, or UN/LOCODE, alongside or instead of custom choices). The "Model(s)" picker groups common content types (DCIM/IPAM/Virtualization/Tenancy/Circuits/Wireless/VPN) with checkboxes, plus free-text entry for anything not listed. Type-specific fields (choice set, related object type, validation rules) only appear for the field types they actually apply to, same as NetBox conditionally showing/hiding form sections.
- **PR-only saves** — identical workflow to device-types: no direct commits, an auto-generated/editable commit message and PR description, the same actor-attribution trailer, and reuse of the same open-PR detection so repeated saves land on the same PR.
- **Import from an instance** — scans the instance's custom fields/choice sets, compares them against the current template, and shows only what's **missing from the template** or **differs from it** (field-by-field, via the same diff engine used for drift). Nothing is pulled in blind: you pick which of those items to import, and only the selected ones get merged into the template — everything else in the template is left untouched. That merged result is then saved as a PR, same as any other save.
- **Push to instance(s)** — creates missing fields/choice sets on selected instances; existing ones (matched by name) are only touched if "Overwrite" is checked. Choice sets are pushed before fields, since a field can reference one by name. Instances tagged "require an approved, merged PR" are gated exactly like device-type pushes.
- **Scope-reduction guard and recovery** — overwrite first previews every object type being removed
  from a field, counts records and meaningful non-default values, and shows samples. Applying requires
  a short-lived signed confirmation plus typing the affected field name(s). A JSON backup is enabled
  by default and downloaded by the browser before apply; opting out requires a second typed warning.
  Backups are never stored by the manager. The restore panel verifies the record hash and instance,
  supports a dry run, re-adds the removed object types, restores each surviving record, and reports
  deleted records as skipped. Backup construction is capped at 100,000 records and 25 MiB.
- **Drift check** — for selected instances, reports what's in the template but missing on the instance, what's on the instance but *not* in the template (someone added a field by hand), and what differs between the two.
- **Choice sets cover both custom choices and predefined ones** — `extra_choices` (the actual value/label pairs, i.e. what NetBox calls "custom field choices," consolidated under choice sets since NetBox 3.5) and `base_choices` (referencing one of NetBox's predefined built-in choice sets instead of/alongside custom ones) are both part of the template schema, fetched from instances, diffed, and pushed.
- All of this reuses the device-type module's audit logging, actor trailer, tag-based bulk selection, and approval-gate logic rather than duplicating it — the custom-fields router imports those helpers directly from `routers/device_types.py`.

NetBox 4.6.8 and 4.7.0 source confirm support for the `cf_<field>__empty` filter used to
reduce transfer volume during previews. If an instance rejects that filter, the manager falls back
to paginating `id`, `display`, `url`, and `custom_fields` and counting client-side; the preview shows
which method was used.

### Audit log

Every device-type action — create, update, import, and delete on GitHub, plus every push to a NetBox instance — is logged to the `push_history` table with **who did it**: `actor_sub`, `actor_name`, and `actor_email`, resolved from the session cookie at request time (so it reflects whoever was actually logged in for that specific call, not a static setting). Logging happens on both success and failure, including errors raised before any GitHub/NetBox API call completes, so a rejected or failed action still shows up rather than silently vanishing.

When authentication is explicitly disabled, entries are attributed to `"anonymous (auth disabled)"`
rather than being left blank.

Because every GitHub commit in this app goes through one shared PAT, GitHub's own commit authorship can't show the real person who requested a change — so the backend appends a `Requested via NetBox Manager by: <name> <email>` trailer to every PR body server-side, after whatever the user typed in the editable description field. This happens unconditionally on the backend, so it survives even if the user's local edits to that field happened to remove it.

The **Audit Log** page lists all of this, newest first: timestamp, actor, action type (GitHub save vs. NetBox push), target, device-type path, status, and detail.

## Tenant permission automation

The **Tenant Permissions** page manages GitHub-backed CRUD permission templates, onboards a tenant
to a selected NetBox instance, applies template updates fleet-wide, and decommissions managed
tenants. Template validation cross-checks the canonical relation registry and RBAC blocklist before
anything is applied. Resolved RO/RW grants and metadata are committed on a bot branch and exposed
through a pull request; the target repository's base branch is never written directly.

The page highlights every explicitly unscoped grant, derives read-only permissions automatically
unless a separate RO template is selected, and refuses group-name collisions. It does not alter
group membership: the existing OIDC login automation remains responsible for matching users to
groups. Tenants are selected and tracked by their immutable NetBox numeric ID; names are retained
only for display, so renaming a tenant cannot break or silently broaden a grant.

Only templates and managed-tenant metadata merged into the configured base branch can drive a
NetBox change; content on an open pull-request branch is never applied. Fleet template changes have
a read-only plan step showing creates, updates, deletions, errors, and unscoped grants before the
administrator confirms the operation. NetBox mutations require the administrator role on every
affected instance.

To verify direct tenant relationships against the exact NetBox installation, run the registry
checker inside that installation's Django environment:

```sh
/opt/netbox/venv/bin/python scripts/check_tenant_relations.py \
  --registry config/tenant-relations.yaml
```

The checker validates direct `Tenant` foreign keys only. Indirect paths such as
`device__tenant` and unsupported generic relationships still require manual review; missing
registry entries remain fail-closed.

## Data migration marker tags

With **Tag migrated objects** enabled, a migration creates or reuses
`migrated-from-<source>-created` and `migrated-from-<source>-changed` before processing migration
objects. The executor includes the corresponding tag ID in the original create or update payload,
so tagging does not require a second API call. Mapped and skipped objects remain untouched. Marker
setup is best-effort and records warnings instead of blocking the migration; rollback retains both
tag definitions and does not restore changed objects.

## Known limitations

### Authorization and auditing

- Audit details intentionally omit request bodies and credentials. Generic mutation events record
  the route and status rather than a field-by-field value diff.
- Access-mapping changes apply to the next request; they do not alter a request already in progress.
- On-demand diff previews require the editor role, while passive drift and coverage checks remain
  available to viewers.

### Synchronization and scale

- Device-type drift candidates are inferred from successful push history rather than an explicit
  instance-to-device-type assignment.
- Coverage checks run on demand for one device type instead of precomputing a fleet-wide matrix.
- Custom-field drift checks are on demand and are not part of the scheduled device-type drift job.
- Changing a GitHub target's repository, branch, or paths changes future lookups but does not move
  existing files.
- Repository listings use one recursive Git-tree request. Archive-backed metadata scans stream into
  a spooled temporary file and enforce compressed, uncompressed, and per-YAML size limits.
- NetBox token-expiry reporting is best-effort and can be unknown when the token cannot uniquely
  read its own token record.

## Feedback

Use the GitHub issue forms to [report a bug](https://github.com/SaschaSchwarzK/netbox-manager/issues/new?template=bug_report.yml)
or [request a feature](https://github.com/SaschaSchwarzK/netbox-manager/issues/new?template=feature_request.yml).
Search existing issues first and never include API tokens, credentials, session cookies, or other
sensitive information.
