# API Routers Documentation

Overview of every FastAPI router in `backend/app/routers/`. Each section covers the
router's purpose, base prefix, and its endpoints.

- [1. Audit](#1-audit)
- [2. Auth](#2-auth)
- [3. Custom Fields](#3-custom-fields)
- [4. Device Types](#4-device-types)
- [5. Drift](#5-drift)
- [6. Fleet](#6-fleet)
- [7. Instances](#7-instances)
- [8. Search](#8-search)
- [9. Syslog](#9-syslog)

---

## 1. Audit

**File:** `backend/app/routers/audit.py`
**Prefix:** `/api/audit` | **Tag:** `audit`

Read-only endpoint that exposes the audit log of device-type/custom-field push
history (`models.DeviceTypePushHistory`).

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/audit` | List the most recent push-history entries (default limit 200) |

**`GET /api/audit`**

- **Params:** `limit: int = 200`
- **Returns:** `list[AuditLogEntryOut]`
- **Behavior:**
  - Queries `DeviceTypePushHistory` ordered by `created_at` descending.
  - For each entry, resolves the `repo_target_id` to a `GithubTarget` for
    display; deleted targets are shown as `"(deleted target)"`.
  - Output includes: id, created_at, action_type (`target_type`), target_name,
    repo target id/name, file_path, status, detail, and actor
    (sub/name/email).

---

## 2. Auth

**File:** `backend/app/routers/auth.py`
**Prefix:** `/api/auth` | **Tag:** `auth`

OIDC (OpenID Connect) login flow for the application, backed by Authlib's `oauth`
provider. Session authentication is a server-side cookie (`SESSION_COOKIE`);
actor attribution for write endpoints comes from the session cookie via
`get_current_actor` / `get_current_user_optional`.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/auth/login` | Redirect to OIDC authorization endpoint |
| GET | `/api/auth/callback` | OIDC redirect back; validate token, create session, redirect home |
| POST | `/api/auth/logout` | Clear the session cookie |
| GET | `/api/auth/me` | Current user / auth status |

**`GET /api/auth/login`**

- **Behavior:** If OIDC is not configured (`oauth` is `None`, i.e.
  `NBM_OIDC_ISSUER` etc. not set), returns 503. Otherwise initiates the
  authorization redirect to `settings.oidc_redirect_uri`.

**`GET /api/auth/callback`**

- **Behavior:**
  - 503 if OIDC is not configured.
  - Runs `oauth.oidc.authorize_access_token(request)`; Authlib validates the
    ID token's signature (against provider JWKS), issuer, audience, expiry, and
    nonce.
  - Extracts `userinfo` and reads groups from the configured
    `settings.oidc_groups_claim` (coerces a string group into a single-item list).
  - Builds the user dict: `sub`, `email`, `name` (falling back to
    `preferred_username`, then `email`, then `sub`), `groups`.
  - Sets an `HttpOnly` session cookie (`Secure` per `settings.session_cookie_secure`,
    `SameSite=Lax`, `max_age=SESSION_MAX_AGE`) via `create_session_cookie(user)`,
    then redirects to `/`.
  - OAuth validation errors → 400 with the error message.

**`POST /api/auth/logout`**

- Deletes the session cookie; returns `{"ok": true}`.

**`GET /api/auth/me`**

- Returns:
  - OIDC disabled: `{"auth_enabled": false, "authenticated": true, "user": null}`
  - OIDC enabled: `{"auth_enabled": true, "authenticated": <bool>, "user": <dict|null>}`

---

## 3. Custom Fields

**File:** `backend/app/routers/custom_fields.py`
**Prefix:** `/api/repos/{target_id}/custom-fields` | **Tag:** `custom-fields`

Manages the NetBox **custom fields template** (a single YAML file per GitHub
target, path `target.custom_fields_path`): view, save, import from a NetBox
instance, diff (drift), and push to instances.

Helpers imported from `device_types.py`: `_get_target`, `_github_error_to_http`,
`_log_action`, `_resolve_instances`, `_with_actor_trailer` (appends the
server-side "Requested via NetBox Manager by: ..." accountability trailer to
PR bodies).

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/repos/{target_id}/custom-fields/file` | Get the custom-fields template file |
| PUT | `/api/repos/{target_id}/custom-fields/file` | Save/validate the template and open a PR |
| POST | `/api/repos/{target_id}/custom-fields/import-scan` | Scan an instance for missing/changed fields |
| POST | `/api/repos/{target_id}/custom-fields/import` | Import selected fields into the template |
| POST | `/api/repos/{target_id}/custom-fields/push` | Push template to NetBox instances |
| POST | `/api/repos/{target_id}/custom-fields/diff` | Drift check for selected instances |

**`GET .../file`** — `CustomFieldsTemplateOut`

- Checks `file_exists` in the target repo. If the file doesn't exist, returns
  `exists=False` with an empty template payload (`CustomFieldsTemplate().to_yaml_dict()`).
- If it exists: resolves the working branch, fetches the file, and also fetches
  `open_pr` (the open PR touching that file path, if any).
- Returns repo_target_id, path, `exists`, `sha`, `payload`, `open_pr`.

**`PUT .../file`** — `SaveResult`

- Validates the submitted payload against `CustomFieldsTemplate` (422 on
  schema mismatch).
- Default commit message includes the field count and choice-set count.
- Opens a PR via `github_repo.save_file` with the actor trailer; conflicts
  (`ValueError`) → 409, other GitHub errors → mapped via
  `_github_error_to_http`.
- Records a success or error entry in the audit log.

**`POST .../import-scan`** — `CustomFieldsImportScanResult`

- Compares a single instance's custom fields/choice sets (fetched via
  `netbox_customfields.get_existing_custom_fields`) against the template using
  `diff_mod.diff_custom_fields_template` — the same diff engine the drift check
  uses.
- Returns only the *candidates*: `extra_on_instance` (missing from template) and
  `changed` (differ), as `ImportCandidate` entries (kind, name, status, payload,
  field_changes).
- Errors fetching from the instance → 502 with the error message.

**`POST .../import`** — `SaveResult` (201)

- Merges **only the selected** items (custom fields and/or choice sets) from a
  freshly re-fetched instance into the template (add if missing, replace if
  changed); unselected items are untouched.
- Re-fetches both template and instance state (doesn't trust the scan step).
- Validates the merged template (422 on failure); 400 if nothing was selected
  or all selections are no longer on the instance.
- Builds a PR body listing imported and skipped items; opens the PR; logs
  audit entry with count.

**`POST .../push`** — `list[PushResultItem]`

- Fetches the template, resolves target instances by `instance_ids`/`tags`.
- Per instance:
  - If `requires_approved_pr`: verifies a merged, approved PR for the template;
    blocks (error + audit log) if no merged PR or PR lacks an approving review.
  - Pushes via `netbox_customfields.push_custom_fields(..., overwrite)`;
    failures become an error result.
  - Each outcome is recorded in `DeviceTypePushHistory`; commits at the end.

**`POST .../diff`** — `list[InstanceCustomFieldsDiffResult]`

- Drift check for selected instances: what's in the template but missing, what's
  there but not in the template, and what changed.
- Runs in a `ThreadPoolExecutor(max_workers=8)`; per-instance errors are
  returned as `error` fields instead of failing the whole call; results are
  re-sorted into the original instance order.

---

## 4. Device Types

**File:** `backend/app/routers/device_types.py`
**Prefix:** `/api/repos/{target_id}/device-types` | **Tag:** `device-types`

The largest router. Manages individual **device-type YAML files** in a GitHub
target repo (path from `target.path_pattern`, e.g.
`device-types/{manufacturer}/{slug}.yml`): list, view, create, update, delete,
import, bulk import, import-from-NetBox, coverage, drift diff, and push.

Shared helpers defined here (also reused by custom-fields):

- `_get_target(target_id, db)` — 404 if the target doesn't exist.
- `_base_dir(target)` — base directory of the path pattern (strips the template suffix).
- `_github_error_to_http(exc)` — maps `RepoAccessError` → 404,
  `GithubException` → its status (or 502), else 500.
- `_with_actor_trailer(pr_body, actor)` — appends
  `\n\n---\nRequested via NetBox Manager by: <who>` to the PR body
  (accountability trailer that can't be dropped by the user).
- `_log_action(...)` — records a `DeviceTypePushHistory` audit row and commits.
- `_resolve_instances(db, instance_ids, tags)` — matches instances by ID and/or
  overlapping tags (empty list if neither filter given).

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/repos/{target_id}/device-types` | List device types in the repo |
| GET | `/api/repos/{target_id}/device-types/file` | Get a device-type file |
| POST | `/api/repos/{target_id}/device-types` | Create a new device type (PR) |
| PUT | `/api/repos/{target_id}/device-types/file` | Save a device type (PR) |
| DELETE | `/api/repos/{target_id}/device-types/file` | Delete a device type (PR) |
| POST | `/api/repos/{target_id}/device-types/import` | Import a single device-type YAML (PR) |
| GET | `/api/repos/{target_id}/device-types/file/coverage` | Coverage of a device type across all instances |
| POST | `/api/repos/{target_id}/device-types/bulk-import/scan` | Scan a source repo for importable .yml files |
| POST | `/api/repos/{target_id}/device-types/bulk-import` | Bulk import from a source git repo (one PR) |
| POST | `/api/repos/{target_id}/device-types/import-from-netbox/scan` | List device types on a NetBox instance |
| POST | `/api/repos/{target_id}/device-types/import-from-netbox` | Bulk import from a NetBox instance (one PR) |
| POST | `/api/repos/{target_id}/device-types/file/diff-with-netbox` | Diff a device type against selected instances |
| POST | `/api/repos/{target_id}/device-types/file/push-to-netbox` | Push a device type to NetBox instances |

**`GET ...`** (list) — `list[DeviceTypeSummary]`

- Lists the device-type directory in the target repo; fetches each file to
  extract `manufacturer`, `model`, `slug`; unparseable files are skipped
  (returned with path only) rather than failing the whole list.

**`GET .../file`** — `DeviceTypeFileOut` (`path` as query param)

- Resolves the working branch, fetches the file, and checks for an open PR
  touching it. Returns sha, payload, `open_pr`.

**`POST ...`** (create) — `SaveResult` (201)

- Validates with the `DeviceType` schema (combining `payload` + manufacturer/model/slug).
- Computes the destination path from `path_pattern`; 400 + audit log if the
  path already exists.
- Opens a PR via `save_file` (sha=None).

**`PUT .../file`** — `SaveResult`

- Validates with `DeviceType` (422 on schema mismatch), opens a PR with the
  supplied `sha` for conflict detection (409 on conflict).

**`DELETE .../file`** — 204

- Deletes the file via a commit/PR (`delete_file`) with the supplied sha;
  default commit message "Remove device-type <path>".

**`POST .../import`** — `SaveResult` (201)

- Parses raw YAML text, validates against the `DeviceType` schema (422 otherwise),
  derives the path from the parsed content, and upserts (fetches existing sha if
  present) with a PR.

**`GET .../file/coverage`** — `list[CoverageEntry]`

- Answers "which instances have this device type and are they up to date?" for
  **all** configured instances (not just those previously pushed).
- Fetches the file payload, then in parallel (8 workers) fetches each instance's
  device type and diffs (`diff_mod.diff_payloads`); per-instance errors are
  returned as `status=error`.

**`POST .../bulk-import/scan`** — `list[BulkImportScanEntry]`

- Cheap scan (single git-trees API call) of the *source* repo's device-type
  directory; manufacturer/slug are **guessed from the path**, not the file
  content. Source repo/branch/dir and optional `source_pat` come from the payload.

**`POST .../bulk-import`** — `BulkImportResult`

- Fetches each selected file from the source repo, validates against
  `DeviceType`, and commits all of them on **ONE** shared branch + **ONE** PR
  (not one PR per file).
- Reports imported, skipped-existing, and failed items; PR body lists each
  category. Pre-validation failures and commit failures are reported, not
  thrown.
- Status: `success` if files were created (PR may still be in `all_failed`),
  `error` if nothing was created.

**`POST .../import-from-netbox/scan`** — `list[ImportFromNetboxScanEntry]`

- 404s early on a bogus repo target or instance; lists all device types on the
  instance via `netbox_client.list_device_types_on_instance` (502 on failure).

**`POST .../import-from-netbox`** — `BulkImportResult`

- Fetches each selected device type (full definition including components) from
  the instance, validates, and lands them on one branch + one PR — the same
  batching as git bulk import, just from NetBox as source.
- Deliberately does **not** push to other NetBox instances: propagation is the
  normal Publish step after review/merge, to preserve PR-only and approval-gate
  guarantees.

**`POST .../file/diff-with-netbox`** — `list[InstanceDiffResult]`

- Drift diff of a single device-type file against selected instances (by
  instance_ids/tags); per-instance errors returned as `error` fields.
  (Synchronous, no thread pool — unlike the parallel coverage/diff endpoints
  in custom-fields.)

**`POST .../file/push-to-netbox`** — `list[PushResultItem]`

- Fetches the device-type file and pushes to selected instances.
- Approval gate per instance (`requires_approved_pr`): blocked if no merged PR
  for the current content, or the merged PR lacks an approving review; each
  block is recorded in the audit log.
- Pushes via `netbox_client.push_device_type(..., overwrite)`; errors become
  error results; all outcomes written to `DeviceTypePushHistory`.

---

## 5. Drift

**File:** `backend/app/routers/drift.py`
**Prefix:** `/api/drift` | **Tag:** `drift`

Drift detection for device-type files. No explicit "this instance runs this
device type" mapping is maintained — instead, candidate `(instance, file_path)`
pairs are inferred from past successful `netbox` pushes in
`models.DeviceTypePushHistory`. For each pair, the source is re-fetched from the
configured GitHub target and the current state is fetched from NetBox, the two
are compared via `diff_mod.diff_payloads`, and the result is upserted into a
`models.DriftRecord`. A scheduled job in `main.py` (`id="drift_check"`) runs a
full check every `NBM_DRIFT_CHECK_INTERVAL_HOURS` (default 6; set to `0` to
disable), and `/api/drift/check-now` provides the on-demand "Check all now"
path used by the Drift page.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/drift` | List all drift records, newest check first |
| POST | `/api/drift/check-now` | Run a full drift check, then list the updated records |

**`GET /api/drift`**

- **Returns:** `list[DriftRecordOut]`
- **Behavior:**
  - Queries `DriftRecord` ordered by `checked_at` descending.
  - For each record, resolves `instance_id`/`repo_target_id` to a
    `NetboxInstance`/`GithubTarget` for display; deleted entities are shown as
    `"(deleted instance)"` / `"(deleted target)"`.
  - `detail_json` is parsed to a dict (unparseable JSON → `{}`); the `diff`
    field is that dict only when it contains a `"status"` key (i.e. it holds a
    real diff result), otherwise `None`.
  - Output includes: id, instance_id, instance_name, repo_target_id,
    repo_target_name, file_path, status, diff, and checked_at.

**`POST /api/drift/check-now`**

- **Returns:** `list[DriftRecordOut]`
- **Behavior:**
  - Calls `drift_mod.run_full_check(db)`, which returns how many pairs were
    checked.
  - Candidate pairs: distinct `(target_name, repo_target_id, file_path)` rows
    in `DeviceTypePushHistory` where `target_type == "netbox"` and
    `status == "success"`, each mapped to a `NetboxInstance` by `target_name`
    (nameless/mapped-out instances are skipped).
  - Per pair (`check_pair`):
    - If the `NetboxInstance` or `GithubTarget` no longer exists → status
      `error` with detail `{"error": "Instance or GitHub target no longer exists."}`.
    - Otherwise decrypts the target's GitHub PAT and fetches the file payload
      (`github_repo.get_file(pat, repo, branch, file_path)`), decrypts the
      instance's NetBox token, fetches the current device type from NetBox
      (`netbox_client.get_existing_device_type` keyed on
      `source.manufacturer`/`source.slug`), and diffs via `diff_mod.diff_payloads`.
    - The diff result's `status` (e.g. `in_sync`/`drift`) and full result dict
      are stored; any exception is caught → status `error` with
      `{"error": <message>}`.
  - Upserts a `DriftRecord` keyed by `(instance_id, repo_target_id, file_path)`:
    existing rows update `status`, `detail_json`, and `checked_at` (UTC); new
    rows are created.
  - Re-queries `DriftRecord` ordered by `checked_at` descending and returns it
    with the same output shape as `GET /api/drift`.

---

## 6. Fleet

**File:** `backend/app/routers/fleet.py`
**Prefix:** `/api/fleet` | **Tag:** `fleet`

Fleet / health status for all configured NetBox instances and GitHub targets.
It answers "which instances can I actually reach, and are their tokens still
valid?" without performing any push or drift work. Instance resolution reuses
`_resolve_instances` (by explicit `instance_ids` and/or overlapping instance
tags, empty when neither is given), so every endpoint accepts the same
filtering contract. GitHub side reads the stored (encrypted) per-target PAT and
asks GitHub for the token's `expiration` header; NetBox side just pings
`/api/v1/` with the instance's stored token. No mutations — read-only.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/fleet/overview` | One summary row per instance/target + aggregate counts |
| POST | `/api/fleet/github/token-status` | Check expiration of the GitHub PATs in the payload |
| POST | `/api/fleet/github/test-connection` | Confirm a GitHub target+branch is reachable with its PAT |
| POST | `/api/fleet/netbox/health` | Ping every selected NetBox instance |
| POST | `/api/fleet/netbox/token-status` | Check expiration of the NetBox instance tokens |
| POST | `/api/fleet/netbox/test-connection` | Confirm selected instances are reachable |

**`GET /api/fleet/overview`** — `FleetOverviewOut`

- Reads **all** `NetboxInstance` rows and all `GithubTarget` rows.
- NetBox side: for each instance, calls `netbox_client.get_health(db, instance)`
  (returns `{"known", "detail", "health"}`):
  - No stored token → `known=False`, detail `"No token configured"`.
  - Token set → `netbox_client.get_instance` (GET `/api/v1/`):
    - 404 → `known=True`, detail `"404 (missing or invalid token/path)"`.
    - 2xx → `known=True`, detail = the instance's `description`, health `instance.id`.
    - Other errors → `known=False`, detail = the error message.
  - Produces one `NetboxHealthStatus` per instance.
- NetBox counts: `netbox_count` (all instances), `netbox_ok` (`known=True`),
  `netbox_error` (`known=False`), `netbox_missing` (no token set).
- GitHub side: for each `GithubTarget`:
  - No token → `known=False`, detail `"No token configured"`.
  - Token set → `github_repo.check_token_expiry(pat)`: hits
    `GET https://api.github.com/rate_limit` with the PAT to read the
    `github-authentication-token-expiration` header. `known` = header was
    read successfully (`None` = no expiration set); `expired` = the header's
    timestamp is already past UTC now.
  - Produces one `FleetGitHubTokenStatus` per target.
- GitHub counts: `github_count` (all targets), `github_ok` (token check
  succeeded), `github_expired` (token expired).
- Output: netbox list + github list + the six aggregate counts.

**`POST /api/fleet/github/token-status`** — `list[FleetGitHubTokenStatus]`

- **Payload:** `{"repo_target_ids": [...]}` (optional subset).
- **No / empty payload:** every target — those without a token return
  `known=False`/`"No token configured"`; those with one are checked
  (`known` = header read, `expired` = header in the past).
- **Non-empty payload:** only the listed targets; unknown IDs → 404
  with a message listing the valid `repo_target_id` values.
- This only reads the GitHub *token* expiration header — it does **not**
  validate that the repo/branch is actually reachable (see
  `test-connection` for that).

**`POST /api/fleet/github/test-connection`** — `GitHubConnectionStatusOut`

- **Payload:** `repo_target_id` (required) + `branch` (required, defaults to
  `main`). Missing → 400; unknown target → 404.
- Decrypts the target's GitHub PAT and calls
  `github_repo.test_connection(pat, repo, branch)`, which checks
  `repo.get_branch(branch)`:
  - Success → `{"ok": true, "detail": "Connected to {repo} ({private|public})"}`.
  - `RepoAccessError` (bad credentials / 404 for repo) → 502 with the detail.
  - `GithubException` 404 → branch not found in the repo; other → GitHub API
    error detail.
- Returns status/detail.

**`POST /api/fleet/netbox/health`** — `list[NetboxHealthStatus]`

- **Payload:** optional `instance_ids`, optional `tags` (instance tags, not
  device-type tags).
- Resolves instances via `_resolve_instances`; none selected → empty list.
- For each, `netbox_client.get_health(db, instance)` (same shape as the
  overview's NetBox side: `known`, `detail`, `health`) → one
  `NetboxHealthStatus` per instance.

**`POST /api/fleet/netbox/token-status`** — `list[NetboxTokenStatus]`

- **Payload:** `instance_ids` (optional subset).
- **No / empty payload:** every instance — no token → `known=False`/
  `"No token configured"`; token set → `github_repo.check_token_expiry(token)`.
- **Non-empty payload:** only the listed instances; unknown IDs → 404 with
  the valid `instance_id` values listed.
- As with the GitHub variant, this reads the token expiration header only;
  it does not ping the instance (see `test-connection`).

**`POST /api/fleet/netbox/test-connection`** — `NetboxConnectionStatusOut`

- **Payload:** `instance_ids` (required, list of ints). Missing → 400.
- Optionally also accepts `tags` for additional instance resolution.
- No resolved instances → 400 with an empty result.
- For each instance:
  - No stored token → 500, detail `"No token configured for instance {name}"`.
  - `netbox_client.get_instance` (GET `/api/v1/`):
    - 2xx → `{"status": "ok"}`.
    - 404 → `status=error`, detail `"404 (missing or invalid token)"`.
    - Other → `status=error`, detail = error message.
- Returns `ok` only when every selected instance responded 2xx; otherwise
  `500` with the first error message.

---

## 7. Instances

**File:** `backend/app/routers/instances.py`
**Prefix:** `/api/instances` | **Tag:** `instances`

CRUD for **NetBox instance** configurations (the NetBox deployments the app
talks to). Each instance stores its base URL, an encrypted API token, SSL
verification flag, tags, and a `requires_approved_pr` flag. The token is
stored encrypted via `crypto.encrypt` and **never** returned in the
`NetboxInstanceOut` response.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/instances` | List all instances (ordered by name) |
| POST | `/api/instances` | Create an instance (201) |
| PATCH | `/api/instances/{instance_id}` | Update an instance (partial fields) |
| DELETE | `/api/instances/{instance_id}` | Delete an instance (204) |
| POST | `/api/instances/{instance_id}/test` | Test the stored connection for an existing instance |
| POST | `/api/instances/test` | Test a connection before saving (Add-instance form) |

**`GET /api/instances`** — `list[NetboxInstanceOut]`

- Returns all instances ordered by `name`.
- Each row is `NetboxInstanceOut`:
  `id`, `name`, `base_url`, `verify_ssl`, `description` (null),
  `tags` (list), `requires_approved_pr`, `created_at`, `updated_at`
  (`api_token` is intentionally omitted).

**`POST /api/instances`** — `NetboxInstanceOut` (201)

- Payload: `NetboxInstanceCreate`
  (`name`, `base_url`, `api_token`, `verify_ssl` (default `True`),
  `description` (optional), `tags` (default `[]`),
  `requires_approved_pr` (default `False`)).
- If a row with the same `name` exists (name is unique), returns **400** —
  "An instance with that name already exists."
- Normalizes `base_url` via `rstrip("/")`.
- Encrypts `api_token` with `crypto.encrypt` into `api_token_encrypted`.
- Creates the `NetboxInstance`, commits, refreshes, and returns the new row.

**`PATCH /api/instances/{instance_id}`** — `NetboxInstanceOut`

- Path param `instance_id` (string). 404 "Instance not found." if the row
  doesn't exist.
- Payload: `NetboxInstanceUpdate` (all fields optional). Any provided field
  is applied:
  - `name`
  - `base_url` (again `rstrip("/")`)
  - `api_token` (re-encrypted into `api_token_encrypted`)
  - `verify_ssl`
  - `description`
  - `tags`
  - `requires_approved_pr`
- Commits, refreshes, returns the updated row.

**`DELETE /api/instances/{instance_id}`** (204)

- 404 "Instance not found." if missing.
- Deletes the row, commits; no body returned.

**`POST /api/instances/{instance_id}/test`** — `ConnectionTestResult`

- 404 if the instance doesn't exist.
- Decrypts the stored `api_token_encrypted`.
- Calls `netbox_client.test_connection(base_url, token, verify_ssl)`, which
  GETs `{base_url}/api/status/` with `Authorization: Token {token}`
  (10s timeout, `verify=verify_ssl`):
  - HTTP 200 → `{"ok": true, "netbox_version": <data["netbox-version"]>, "detail": null}`
  - non-200 → `{"ok": false, "netbox_version": null, "detail": "HTTP {status}: {body[:200]}"}`
  - network/request error → `{"ok": false, "netbox_version": null, "detail": str(exc)}`

**`POST /api/instances/test`** — `ConnectionTestResult`

- Tests a connection **before** saving — used by the "Add instance" form.
- Payload: `NetboxInstanceCreate` (uses `base_url` `rstrip("/")`, the raw
  `api_token` from the form, and `verify_ssl`).
- Same `netbox_client.test_connection` behavior as above
  (returns `ok`/`netbox_version`/`detail`).

---

## 8. Search

**File:** `backend/app/routers/search.py`
**Prefix:** `/api/search` | **Tag:** `search`

Cross-instance NetBox search for devices, virtual machines, virtual device
contexts (VDCs), IP addresses, prefixes, and MAC addresses. A single
`GET /api/search` fans out to the selected NetBox instances in parallel and
aggregates the results. It uses NetBox's built-in `q` quick-search filter
where available (name, serial, asset tag, address, etc.), plus an exact
`mac_address` filter for MACs, since NetBox's interface filter doesn't
support partial MAC matching.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/search` | Search one or more configured instances for matching objects |

**`GET /api/search`** — `SearchResponse`

- Query params:
  - `query` — required, `min_length=1`. The search string.
  - `instance_ids` — optional list of instance IDs. When omitted, **all**
    configured instances are searched.
- Loads all `NetboxInstance` rows; if `instance_ids` is provided, keeps only
  those whose ID is in the list. If no instances remain, returns **400** —
  "No matching NetBox instances configured."
- Runs one search task per instance via `ThreadPoolExecutor(max_workers=8)`
  (`run_one`):
  - Decrypts the instance's stored `api_token_encrypted`.
  - Calls `netbox_client.search_instance(base_url, token, verify_ssl, query)`;
    on success wraps the returned dict into an `InstanceSearchResult` with all
    six collections populated.
  - Any exception during the search is caught: the result row is returned with
    `error: str(exc)` and empty collections, so a single unreachable instance
    never fails the whole request.
- Result rows are sorted back into the original instance order (independent
  of which instance responded first).
- Returns `SearchResponse({query, results})`.

**`InstanceSearchResult`** fields — per instance:

- `instance_id`, `instance_name`
- `devices`, `virtual_machines`, `virtual_device_contexts`,
  `ip_addresses`, `prefixes`, `mac_addresses` — lists of `SearchResultItem`
  (defaults `[]`; empty on error)
- `error` — `null` on success, `str(exc)` if that instance failed

**`SearchResultItem`** fields — per found object:

- `id`, `name`, `serial`, `type_display`, `site`, `status`, `url`

**Per-collection search behavior** (`netbox_client.search_instance`):

- **Devices** (`nb.dcim.devices.filter(q=query)`):
  `name` falls back to `(unnamed device #{id})`; `type_display` = device type
  display name; `url` → `{base_url}/dcim/devices/{id}/`.
- **Virtual machines** (`nb.virtualization.virtual_machines.filter(q=query)`):
  `serial` always null; `type_display` = role; `url` →
  `{base_url}/virtualization/virtual-machines/{id}/`.
- **Virtual device contexts** (`nb.dcim.virtual_device_contexts.filter(q=query)`):
  wrapped in `try/except pynetbox.RequestError` — NetBox versions before 4.1
  lack this endpoint and it's treated as "none found" rather than an error.
- **IP addresses** (`nb.ipam.ip_addresses.filter(q=query)`):
  `name` = the IP address; `type_display` = assigned object or `(unassigned)`;
  `url` → `{base_url}/ipam/ip-addresses/{id}/`.
- **Prefixes** (`nb.ipam.prefixes.filter(q=query)`):
  `name` = the CIDR prefix; `type_display` = role;
  `url` → `{base_url}/ipam/prefixes/{id}/`.
- **MAC addresses** — three passes, each wrapped in `try/except pynetbox.RequestError`
  (invalid MAC format or missing endpoint → skip, not an error):
  1. `nb.dcim.interfaces.filter(mac_address=query)` — physical interfaces;
     `type_display` → `Interface: {device}/{iface.name}`.
  2. `nb.virtualization.interfaces.filter(mac_address=query)` — VM interfaces;
     `type_display` → `VM interface: {vm}/{iface.name}`.
  3. `nb.dcim.mac_addresses.filter(mac_address=query)` — NetBox 4.2+
     dedicated MAC Address objects; `type_display` → `Assigned to: ...` or
     `(unassigned)`.

---

## 9. Syslog

**File:** `backend/app/routers/syslog.py`
**Prefix:** `/api/syslog` | **Tag:** `syslog`

Read-only endpoints for the **syslog forwarding** configuration of the audit
log. Forwarding is deliberately config-file only (the `NBM_SYSLOG_*` env vars,
see `.env.example`) — it is not editable from the UI at runtime. `GET
/api/syslog/settings` exposes the current settings read-only, and `POST
/api/syslog/test` sends a single test message to the configured server so the
infrastructure wiring can be verified without waiting for real pushes.

The actual forwarding is not done by these endpoints at all. It is hooked onto
the ORM: `syslog_client._forward_audit_entry_to_syslog` is a SQLAlchemy
`after_insert` event on `models.DeviceTypePushHistory` (registered at import
time in `main.py`), so every audit entry — every GitHub save and NetBox push,
success or failure — is forwarded automatically, once, in one place, even if
a new audit-logging call site is added later.

### Endpoints

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/syslog/settings` | Get the current syslog forwarding settings (read-only) |
| POST | `/api/syslog/test` | Send a test syslog message to the configured server |

**`GET /api/syslog/settings`** — `SyslogSettingsOut`

- No params, no mutations. Returns a snapshot of the `settings` module:
  - `enabled` — whether forwarding is on (`NBM_SYSLOG_ENABLED`)
  - `protocol` — `udp` or `tcp` (`NBM_SYSLOG_PROTOCOL`, default `udp`)
  - `host` — syslog server host (`NBM_SYSLOG_HOST`, default empty)
  - `port` — syslog port (`NBM_SYSLOG_PORT`, default `514`)
  - `facility` — syslog facility keyword, e.g. `local0` (`NBM_SYSLOG_FACILITY`)
  - `app_name` — app name in the syslog header (`NBM_SYSLOG_APP_NAME`, default `netbox-manager`)
- Changing these requires setting the environment variables and restarting
  the backend; there is no write path for this router.

**`POST /api/syslog/test`** — `SyslogTestResult`

- No payload. Sends one test message via `syslog_client.send_test_message()`.
- Behavior:
  - Syslog disabled (`NBM_SYSLOG_ENABLED` not set) →
    `{ok: false, detail: "Syslog forwarding is disabled (NBM_SYSLOG_ENABLED is not set)."}`.
  - No host configured (`NBM_SYSLOG_HOST` empty) →
    `{ok: false, detail: "No syslog host configured (NBM_SYSLOG_HOST is empty)."}`.
  - Otherwise builds a test RFC 5424 message (`action_type: "test"`,
    `target: "-"`, `status: "success"`,
    detail `"Test message from NetBox Manager."`) and sends it:
    - Delivery succeeds →
      `{ok: true, detail: "Sent via {PROTOCOL} to {host}:{port}."}`
      (protocol uppercased, e.g. `UDP`/`TCP`).
    - Delivery fails → `{ok: false, detail: <str(exc)>}` (e.g. connection
      refused, timeout, DNS failure).

**How an audit entry is forwarded** (not an endpoint — the `after_insert`
hook on `DeviceTypePushHistory`)

- `send_audit_entry(entry)`:
  - No-ops (returns early) if `syslog_enabled` is false or `syslog_host` is
    empty.
  - Builds the message (below) and calls `send_raw`.
  - `send_raw` raises on failure, but `send_audit_entry` catches and swallows
    all exceptions — a syslog delivery failure must never break the actual
    audit-log write or the request that triggered it.
- Transport (3s socket timeout):
  - `tcp` — `socket.create_connection` then `sendall` with a trailing newline
    (RFC 6587 non-transparent framing).
  - `udp` — single `sendto` on an ephemeral socket.

**Message format** — RFC 5424:

```
<pri>1 <timestamp> <hostname> <app_name> <procid> <msgid> [auditEntry@32473 actor="..." actorEmail="..." action="..." target="..." filePath="..." status="..."] <detail>
```

- `pri` = facility code × 8 + severity; severity is 6 (informational) when
  `status == "success"`, 4 (warning) otherwise. Facility comes from the
  `FACILITY_CODES` map for `settings.syslog_facility`, falling back to
  `local0`.
- `timestamp` — UTC with milliseconds and a `Z` suffix.
- `hostname` — truncated to 255 chars; `app_name` — truncated to 48 chars
  (default `netbox-manager`); `procid` — the process id; `msgid` — the
  `action_type` truncated to 32 chars (default `audit`).
- Structured-data values: `None` → `-`, and `\`, `"`, `]` are escaped.
- The free-text `detail` is the MSG part of the message.
~~~


