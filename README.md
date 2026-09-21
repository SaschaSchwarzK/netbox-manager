# NetBox Manager

A web UI for managing device-type definitions across multiple NetBox instances.
**Device-type YAML files live directly in a GitHub repo** (e.g. a fork of
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library)) —
the repo is the source of truth, and every save from the editor is a git commit (or PR).
The app's own config (NetBox instances, GitHub repo targets, push history) is kept in a small
local SQLite database.

## Stack

- **Backend:** FastAPI + SQLAlchemy (SQLite) + `pynetbox` for the NetBox API + `PyGithub` for reading/committing device-type files.
- **Frontend:** React + TypeScript + Vite.
- **Deployment:** Docker Compose (backend, frontend/nginx — no separate database service to run).

## Quick start

1. Generate an encryption key for stored NetBox tokens / GitHub PATs:

   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. Copy the backend env file and paste the key in:

   ```bash
   cp backend/.env.example backend/.env
   # edit backend/.env and set NBM_SECRET_KEY to the generated value
   ```

3. Build and start everything:

   ```bash
   docker compose up --build
   ```

4. Open the UI at http://localhost:8080. The API is at http://localhost:8000/docs (Swagger UI).
   Hitting http://localhost:8000/ directly with no path will show a small JSON banner — that's
   expected, the actual app lives behind the frontend on :8080 (or under /docs for the API).

## How device-types are stored

There is **no database table for device-types**. Instead:

- Under **GitHub Targets**, configure a repo + branch + path pattern (e.g. `myorg/devicetype-library`, `main`, `device-types/{manufacturer}/{slug}.yml`) and a PAT with repo access.
- The **Device Types** page lists every `.yml`/`.yaml` file under that path directly from GitHub (via the git trees API), and clicking one loads it straight from the repo into the editor.
- "New from scratch" and "Import YAML" commit a new file immediately.
- Saving in the editor is a commit — optionally routed through a PR instead of committing straight to the branch. GitHub's file SHA is used for optimistic concurrency, so if the file changed on GitHub since you loaded it, the save is rejected with a clear message rather than silently overwriting someone else's edit.
- The SQLite DB only holds NetBox instance configs (encrypted tokens), GitHub target configs (encrypted PAT), and a push-history log — no device-type content.

One consequence: listing device types fetches and parses every file in the repo to show manufacturer/model/slug in the table, so it scales fine for an internal library of tens–low hundreds of device types, but would be slow against something the size of the full community library (thousands of files). Worth adding caching or a lighter-weight listing (e.g. parsing just the front-matter, or paginating) if you point this at a very large repo.

## What's implemented

- **NetBox instance management** — add/edit/remove instances, tokens encrypted at rest, "Test connection" hits `/api/status/`. Each instance can carry free-form **tags** (e.g. `prod`, `region:eu`) and an optional **"require an approved, merged PR"** flag.
- **GitHub target management** — configure a repo/branch/path pattern and PAT; this doubles as both the source and destination for device-type files.
- **Device-type editor** — tabs for base attributes, interfaces, console ports/server ports, power ports/outlets, rear/front ports, device bays, and module bays. Each component tab is an editable grid with a bulk-add pattern helper (e.g. `GigabitEthernet1/0/{1-48}`) and a live YAML preview.
- **Import** — upload a `.yml`/`.yaml` file; it opens as an in-memory, unsaved draft (nothing is written to GitHub until the first save).
- **PR-only workflow** — there is no direct-commit path. Every save opens (or adds a commit to) a pull request, with an auto-generated, editable commit message and PR description summarizing what changed (component counts, added/removed fields). Re-saving a device type that already has an open PR adds to that same PR instead of forking a new one.
- **Cross-instance search** (**Search** page) — finds devices, virtual machines, virtual device contexts, IP addresses, prefixes, and MAC addresses by name, serial, address, or MAC, fanned out across every selected NetBox instance in parallel. Devices/VMs/VDCs/IPs/prefixes use NetBox's `q` quick-search filter; MAC lookups use an exact `mac_address` filter (against both interface-level MACs and, on NetBox 4.2+, the dedicated MAC Address object) since NetBox doesn't support partial MAC matching. A malformed MAC/IP query is caught and treated as "no results" for that lookup rather than failing the whole search.

### Change safety across many instances

- **Diff-before-push** — "Preview diff" in the Publish tab compares the GitHub source against what's actually deployed on each selected instance (base fields + per-component added/removed/changed), so a push's effect is visible before it happens.
- **Approval gate** — instances flagged "require an approved, merged PR" are checked at push time: the backend looks up the PR that produced the current file content and confirms it was merged *and* has at least one approving review. A push to such an instance is refused with a clear reason if that's not the case.
- **Bulk/grouped push** — instances can carry tags, and the Publish tab has "select by tag" buttons (e.g. click `region:eu` to select every instance with that tag) plus an "all" button, so pushing to a whole group is one click instead of checking boxes individually. The API also accepts a `tags` array directly for scripted/bulk pushes.
- **Drift detection** — a background job (interval set by `NBM_DRIFT_CHECK_INTERVAL_HOURS`, default 6; set to `0` to disable) periodically re-diffs every instance/device-type pair that's had a successful push in the past, flagging anything that's drifted from the GitHub source (hand-edited in NetBox's UI, partially failed push, etc.). Results are on the **Drift** page, with a "Check all now" button for an on-demand run.

### Login (OIDC)

The app is fully open (no login) until `NBM_OIDC_ISSUER` is set — useful for local development, but **set it before exposing this beyond a trusted network.**

Once configured:
- `GET /api/auth/login` starts the standard OIDC authorization-code flow (via [Authlib](https://docs.authlib.org/)), redirecting to your provider.
- `GET /api/auth/callback` exchanges the code for tokens; Authlib validates the ID token's signature (against the provider's published JWKS), issuer, audience, expiry, and nonce as part of that exchange — this app doesn't hand-roll any of that verification.
- The verified claims (`sub`, `email`, `name`, and the group-membership claim named by `NBM_OIDC_GROUPS_CLAIM`) are signed into an httponly session cookie (`itsdangerous`, 12h expiry) — there's no server-side session store, so any backend replica can validate the cookie independently.
- Every `/api/*` route except `/api/auth/*` and `/api/health` requires a valid session once OIDC is configured; unauthenticated requests get a plain 401.
- The frontend checks `/api/auth/me` on load: if not authenticated, it shows a sign-in screen instead of the app; once signed in, the sidebar shows the user's name and groups with a sign-out button.

**What this does not do yet** (planned next): map groups to app roles or scope which NetBox instances/GitHub targets a group can see — right now, any authenticated user (regardless of group) has full access to everything, same as before login existed. The groups claim is captured and available in the session (and visible in the sidebar) precisely so that mapping can be layered on without another round of provider configuration.

Setting this up requires registering this app with your identity provider as a confidential web app/client, with `NBM_OIDC_REDIRECT_URI` as an allowed redirect URI. See `.env.example` for the full list of variables and provider-specific notes on the groups claim (Keycloak, Entra ID/Azure AD, and Okta all expose group membership differently).

### Fleet visibility

- **Instance health dashboard** (**Fleet** page) — for every NetBox instance, in parallel: reachable or not, NetBox version, installed plugin versions (all straight from `/api/status/`), and response latency. Useful before pushing a device type that needs a feature only newer NetBox versions support (module bays, virtual device contexts, etc.) — check here first rather than finding out from a failed push.
- **Token expiry warnings** — the same Fleet page checks GitHub PAT expiry (via the `github-authentication-token-expiration` response header GitHub sets on fine-grained and expiring classic PATs) and, best-effort, NetBox token expiry (via `/api/users/tokens/` — only reliable when a token can see exactly one token, itself; if multiple are visible we say so rather than guessing which one is active). Badges flag anything expired or expiring within 14 days.
- **Cross-instance coverage** — on the Device Types list, "Check coverage" per row answers "which instances have this device type, and are they up to date?" across *every* configured instance, not just ones it's been pushed to before (that's Drift) or a subset you've selected (that's the push-time diff preview).

- **Bulk import** (**Bulk Import** page) — two source types, selected via a tab: a **GitHub library** (scans a source repo's device-type directory, defaults to `netbox-community/devicetype-library` but works against any fork; scanning is cheap — one git-trees API call, with manufacturer/slug in the picker guessed from the file path rather than fetched from content, since fetching content for every one of the ~4000 files in the real community library would be slow and rate-limit-hungry) or a **NetBox instance** (scans every device type already defined there, full detail fetched only for what you select). Either way you filter/multi-select from the results and land everything in **one shared branch and one pull request** against your target repo rather than flooding it with one PR per file — both source types reuse the same underlying commit/PR machinery. Files that already exist at their destination path are skipped rather than overwritten. Propagating an import on to *other* NetBox instances is a deliberate separate step, once the PR is reviewed and merged, using the normal Publish tab — pushing straight out of an unreviewed import would bypass the PR-only and approval-gate guarantees the rest of the app relies on.

## Troubleshooting

- **`frontend-1 exited with code 1` / nginx: "host not found in upstream 'backend'"** — this means the **backend** container crashed (nginx can't even resolve the `backend` hostname if that container never came up), and nginx's default behavior is to refuse to start at all if a `proxy_pass` hostname can't be resolved at config-load time. `nginx.conf` now resolves the backend lazily (via a `resolver` + variable), so the frontend starts and serves fine even if the backend is down, returning a 502 for `/api/` instead of crash-looping — check the **backend** container's logs for the actual failure. The most common cause is the one below.
- **Backend crashes on startup with a Fernet/base64 error** — this means `NBM_SECRET_KEY` in `backend/.env` is still the placeholder value (or `backend/.env` was never created from `backend/.env.example` at all). The startup error now names this explicitly and tells you the exact command to generate a real key; see Quick start above.

### Syslog forwarding

The audit log can be forwarded to a remote syslog server as structured **RFC 5424** messages — config-file only, on purpose: the **Syslog** page shows the current settings read-only plus a "Send test message" button, but changing where audit data goes isn't something the UI lets you do at runtime.

- `NBM_SYSLOG_ENABLED`, `NBM_SYSLOG_PROTOCOL` (`udp` or `tcp`), `NBM_SYSLOG_HOST`, `NBM_SYSLOG_PORT`, `NBM_SYSLOG_FACILITY` (standard syslog facility keyword, e.g. `local0`), `NBM_SYSLOG_APP_NAME` — see `.env.example`.
- Every audit entry (every GitHub save and NetBox push, success or failure) is forwarded automatically — this is wired as a SQLAlchemy `after_insert` event on the audit table itself, not called explicitly at each of the several places that write one, so a future audit-logging call site can't accidentally forget to forward.
- Message severity is informational for successful actions, warning for failed ones. Structured data carries actor name/email, action type, target, file path, and status as named fields (`[auditEntry@32473 actor="..." action="..." ...]`); the free-text detail becomes the MSG.
- A syslog delivery failure (unreachable host, connection refused) never breaks the actual audit-log write or the request that triggered it — it's caught and silently dropped for the automatic path, while the test-button path surfaces the real error so misconfiguration is visible when you're deliberately checking it.
- TCP framing is newline-delimited (RFC 6587 "non-transparent framing"), which is what most syslog receivers (rsyslog, syslog-ng) expect out of the box.

### Custom-fields template

A second GitHub-backed workflow, parallel to device-types: a single YAML file per repo (`custom_fields_path` on the GitHub target, default `custom-fields/template.yml`, in its own folder separate from device-types) listing every custom field and custom field choice set that should exist across the fleet.

- **Custom Fields** page — proper create/edit forms for both custom fields and choice sets, grouped into the same sections NetBox's own forms use (General / Values / Behavior / Validation Rules for fields; Name/Base Choices/Extra Choices/Order for choice sets), not a flat spreadsheet. Field coverage now matches NetBox's actual model, including several previously-missing attributes verified against NetBox's own source and docs: `unique`, `search_weight`, `comments`, `related_object_type` and `related_object_filter` (for object/multiobject fields), and `base_choices` (referencing a predefined choice set like IATA airport codes, ISO 3166 country codes, or UN/LOCODE, alongside or instead of custom choices). The "Model(s)" picker groups common content types (DCIM/IPAM/Virtualization/Tenancy/Circuits/Wireless/VPN) with checkboxes, plus free-text entry for anything not listed. Type-specific fields (choice set, related object type, validation rules) only appear for the field types they actually apply to, same as NetBox conditionally showing/hiding form sections.
- **PR-only saves** — identical workflow to device-types: no direct commits, an auto-generated/editable commit message and PR description, the same actor-attribution trailer, and reuse of the same open-PR detection so repeated saves land on the same PR.
- **Import from an instance** — scans the instance's custom fields/choice sets, compares them against the current template, and shows only what's **missing from the template** or **differs from it** (field-by-field, via the same diff engine used for drift). Nothing is pulled in blind: you pick which of those items to import, and only the selected ones get merged into the template — everything else in the template is left untouched. That merged result is then saved as a PR, same as any other save.
- **Push to instance(s)** — creates missing fields/choice sets on selected instances; existing ones (matched by name) are only touched if "Overwrite" is checked. Choice sets are pushed before fields, since a field can reference one by name. Instances tagged "require an approved, merged PR" are gated exactly like device-type pushes.
- **Drift check** — for selected instances, reports what's in the template but missing on the instance, what's on the instance but *not* in the template (someone added a field by hand), and what differs between the two.
- **Choice sets cover both custom choices and predefined ones** — `extra_choices` (the actual value/label pairs, i.e. what NetBox calls "custom field choices," consolidated under choice sets since NetBox 3.5) and `base_choices` (referencing one of NetBox's predefined built-in choice sets instead of/alongside custom ones) are both part of the template schema, fetched from instances, diffed, and pushed.
- All of this reuses the device-type module's audit logging, actor trailer, tag-based bulk selection, and approval-gate logic rather than duplicating it — the custom-fields router imports those helpers directly from `routers/device_types.py`.

### Audit log

Every device-type action — create, update, import, and delete on GitHub, plus every push to a NetBox instance — is logged to the `push_history` table with **who did it**: `actor_sub`, `actor_name`, and `actor_email`, resolved from the session cookie at request time (so it reflects whoever was actually logged in for that specific call, not a static setting). Logging happens on both success and failure, including errors raised before any GitHub/NetBox API call completes, so a rejected or failed action still shows up rather than silently vanishing.

When OIDC is off, entries are attributed to `"anonymous (auth disabled)"` rather than being left blank — the point is that a blank actor should never be ambiguous between "logging broke" and "auth was off."

Because every GitHub commit in this app goes through one shared PAT, GitHub's own commit authorship can't show the real person who requested a change — so the backend appends a `Requested via NetBox Manager by: <name> <email>` trailer to every PR body server-side, after whatever the user typed in the editable description field. This happens unconditionally on the backend, so it survives even if the user's local edits to that field happened to remove it.

The **Audit Log** page lists all of this, newest first: timestamp, actor, action type (GitHub save vs. NetBox push), target, device-type path, status, and detail.

## Known limitations

- **No Alembic migrations, but schema changes are no longer destructive.** Tables are created with `Base.metadata.create_all()`, and on every startup `app/migrations.py` diffs each mapped table's actual columns against the models and adds anything missing (`ALTER TABLE ADD COLUMN`, with the model's default). It never removes, renames, or retypes a column — only adds — so pulling an update that adds a field no longer requires wiping the SQLite volume. A prior version of this README said otherwise; that's fixed now.
- **Diff/drift now compares full attributes, not just name and type.** `get_existing_device_type()` fetches every field each component template schema actually defines (`maximum_draw`/`allocated_draw` on power ports, `power_port`/`feed_leg` on outlets, `positions` on rear ports, `rear_port`/`rear_port_position` on front ports, `position` on module bays, `poe_mode`/`poe_type` on interfaces, `description` everywhere), and the diff engine (`field_level_diff()`) reports exactly which fields differ on a changed item — e.g. `{"field": "maximum_draw", "source": 60, "existing": 30}` — instead of a bare "this item changed" flag. The same engine backs both device-type diffs and the custom-fields template diff.
- **Drift detection has no explicit instance↔device-type mapping** — it infers candidate pairs from push history, so a device type that's only ever been pushed manually outside this tool won't be tracked.
- **Authentication exists (OIDC), authorization doesn't yet** — every authenticated user has full access to every instance, target, and push action regardless of group. Group-based role/instance scoping is the planned next step (see the "Login (OIDC)" section above). Until then, run this only with an identity provider you trust to gate who gets an account at all.
- **Don't run without `NBM_OIDC_ISSUER` set outside a trusted network** — with it unset, the API has no auth at all, by design (for local dev).
- **Audit log covers device-type actions only** — instance/GitHub-target CRUD and drift checks aren't currently logged with an actor. Extending the same `_log_action`-style pattern to those is straightforward if needed.
- **NetBox token expiry detection is best-effort** — it relies on the token being able to see its own record via `/api/users/tokens/` and being the only one visible. Many tokens won't have permission to that endpoint at all (shows as "unknown"), which is expected, not a bug.
- **Coverage checks are on-demand per device type**, not a pre-computed fleet-wide matrix — checking coverage for every device type in a large repo against every instance would mean N×M live NetBox calls; the per-row button keeps that cost opt-in.
- **Custom-fields drift isn't in the scheduled background job** — the device-type Drift page runs on a timer; the custom-fields drift check is on-demand only (the "Check drift" button), by design, since the request was for "a function to check" rather than continuous monitoring. Wiring it into the same APScheduler job would be a small follow-up.
