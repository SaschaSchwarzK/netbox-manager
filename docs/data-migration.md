# Data Migration Guide

The **Data Migration** feature copies selected infrastructure data from one NetBox instance to
another. It builds a dry-run plan first, matches source objects to existing target objects, lets
you resolve ambiguous matches, and writes only after explicit confirmation.

Use this guide when moving devices, virtual machines, interfaces, IPAM data, or their supporting
reference objects between instances.

## 1. Before you begin

Make sure that:

- Both NetBox instances are configured on the **NetBox Instances** page and are visible to you.
- The source token can read every object type that you intend to migrate.
- The target token can create and update the required object types.
- Your NetBox Manager account has at least the **Editor** role and the **Admin** role for the target
  instance. Viewers can inspect visible jobs and reports but cannot plan or execute a migration.
- The source and target run compatible NetBox versions.
- You have reviewed the source data for duplicate names, slugs, prefixes, and other natural keys.

The source client is read-only. NetBox Manager does not write to the source instance.

> **Screenshot placeholder:** Data Migration page before a migration is configured.
> Suggested file: `docs/images/data-migration/new-migration.png`
>
> <!-- Replace this blockquote with: ![New data migration](images/data-migration/new-migration.png) -->

## 2. Select the source and target

Open **Data Migration** and stay on the **New migration** tab.

1. Select the **Source instance (read-only)**.
2. Select a different **Target instance**.
3. Select **Test connection**.
4. Confirm that both sides show a valid token and an expected NetBox version.

The connection test checks reachability, token validity, and version information. It deliberately
does not test target write permission because there is no safe read-only probe for every write the
migration may need. A successful test therefore does not guarantee that the target token has all
required object permissions.

> **Screenshot placeholder:** Source and target selected with successful connection results.
> Suggested file: `docs/images/data-migration/source-target.png`
>
> <!-- Replace this blockquote with: ![Migration source and target](images/data-migration/source-target.png) -->

## 3. Limit the migration by tenant

The **Tenant filter** is optional.

- Leave it empty to include objects for all tenants.
- Select one or more tenants to include tenant-scoped devices, virtual machines, IPAM objects, and
  their components for those tenants.
- Hold Ctrl on Windows/Linux or Command on macOS to select more than one tenant.
- Enable **Also include untenanted objects** when tenant-filtered data should be combined with
  objects that have no tenant.

Reference objects such as sites, manufacturers, device types, roles, tags, and platforms are not
included merely because they exist. They are pulled into the plan when selected data actually
references them.

## 4. Configure migration options

The source-and-target card also contains these options:

| Option | Effect |
|---|---|
| **Tag migrated objects** | Adds a marker tag derived from the source instance name to supported objects created by the migration. |
| **Max requests/sec (per instance)** | Limits request rate separately for the source and target. Lower it if either NetBox instance is rate-limited or under load. |
| **Stop on first API error** | Stops execution after the first API error instead of continuing with independent pending items. |

The default request rate is `4` requests per second per instance.

## 5. Select data types and understand dependencies

Select each top-level data type that you want to migrate. NetBox Manager maintains two kinds of
selection:

- A normal checked box is a type that you selected directly.
- A highlighted, locked checked box is a selectable type required by another selection.

Required dependencies are selected immediately and transitively. For example, selecting
**Interfaces** also selects and locks **Devices** because every interface requires a device. The
text below the locked type identifies what requires it.

You cannot independently clear a locked dependency while another selected type still requires it.
If you directly selected that dependency earlier, clearing your direct selection leaves it checked
and locked, and a warning explains why. When the last dependent type is cleared, the dependency is
released. It remains selected only if you had selected it directly.

Some checked types show **May also include: ... (if referenced)**. These are optional dependencies.
They are not checked automatically because their inclusion depends on the source objects that are
actually found. They remain a visual hint and do not change your explicit selection.

Non-selectable reference types have no checkbox. The backend still resolves and orders them when
they are required by selected data.

> **Screenshot placeholder:** Interfaces selected, Devices auto-selected and locked, and optional dependency hints visible.
> Suggested file: `docs/images/data-migration/type-dependencies.png`
>
> <!-- Replace this blockquote with: ![Migration type dependencies](images/data-migration/type-dependencies.png) -->

## 6. Choose the conflict policy

The conflict policy controls what happens when NetBox Manager finds an existing target object with
the same natural identity, such as a matching name, slug, or type-specific combination.

| Policy | Result |
|---|---|
| **Leave it unchanged (map only)** | Reuses the existing target object without changing it. This is the safest default. |
| **Update from source** | Updates the matching target object with values from the source. |
| **Update empty fields only** | Fills target fields that are empty without replacing populated values. |

Set the global policy in step 3. A directly selected data type also has its own selector, which can
override the global policy for that type.

Objects that you explicitly map in the mapping review are never changed by the conflict policy.

## 7. Create and review the dry-run plan

Select **Preview migration (dry run)**. Planning runs in the background and does not modify either
NetBox instance.

The summary groups results by object type:

- **Create**: no suitable target object was found.
- **Update**: a target match will be changed according to the conflict policy.
- **Map**: an existing target object will be reused without modification.
- **Skip**: the object will not be migrated.
- **Ambiguous**: more than one possible target match was found and you must decide what to do.
- **Errors**: the object could not be planned successfully.

Review the warnings and the embedded detailed report. Use **Refresh report** while a job is active,
**Open report** for a separate browser view, or **Download report** to retain a copy.

> **Screenshot placeholder:** Completed dry-run summary and detailed report.
> Suggested file: `docs/images/data-migration/plan-summary.png`
>
> <!-- Replace this blockquote with: ![Migration plan summary](images/data-migration/plan-summary.png) -->

## 8. Resolve mappings

The **Mapping review** table shows the source object, an automatically matched target, the proposed
match result, and any manual override.

For a row that needs attention, choose one of these actions:

- **Map to existing** and select the correct target object.
- **Skip** to omit the source object.
- **Create new** to create a separate target object despite possible matches.

Use **Add manual mapping** when you know an object type and source ID that is not convenient to find
in the table. Choose its action and, for a map action, the target object.

Any mapping edit marks the plan as pending. Select **Re-plan mapping changes** to apply the edits to
the dry run. You cannot start execution while mapping changes are pending or ambiguous objects
remain.

> **Screenshot placeholder:** Mapping review with an ambiguous row resolved to an existing target.
> Suggested file: `docs/images/data-migration/mapping-review.png`
>
> <!-- Replace this blockquote with: ![Migration mapping review](images/data-migration/mapping-review.png) -->

## 9. Run the migration

After the plan and mappings are correct:

1. Select **I've reviewed the plan above and want to write these changes to the target instance**.
2. Select **Run migration**.
3. Monitor the status, current step, summary counters, warnings, and detailed report.

The executor respects dependency order, so required reference objects are processed before objects
that refer to them. Progress counters show completed work; pending objects are not included in the
counts.

Use **Cancel** to request cancellation of a running job. Cancellation is cooperative: the current
object operation completes, then execution stops between items.

## 10. Handle completion and errors

Common terminal states are:

| Status | Meaning |
|---|---|
| `completed` | Every planned operation finished successfully. |
| `completed_with_errors` | Execution finished, but one or more items or deferred patches failed. |
| `failed` | Planning or execution could not continue. |
| `cancelled` | A cancellation request stopped execution. |
| `rolled_back` | Rollback finished without recorded deletion errors. |
| `rolled_back_with_errors` | Rollback finished, but at least one deletion failed. |

For `completed_with_errors`, select **Retry failed objects** after correcting the underlying target
permission, validation, connectivity, or data problem. Only failed items and patches are returned
to pending state; completed operations are not blindly repeated.

## 11. Roll back created objects

Use **Rollback created objects** only after execution has stopped. Read the confirmation carefully.

Rollback is best-effort and intentionally limited:

- It deletes objects that this migration successfully created, in reverse dependency order.
- It does not delete objects that were mapped to existing targets.
- It does not restore objects that were updated.
- Patches already applied to surviving objects can remain.
- The marker-tag definition is retained.
- A target-side change made after migration can make deletion fail.

Always review the rollback counters and detailed report. A rollback is not a substitute for a
target backup.

> **Screenshot placeholder:** Completed migration actions, including retry and rollback controls.
> Suggested file: `docs/images/data-migration/completed-actions.png`
>
> <!-- Replace this blockquote with: ![Completed migration actions](images/data-migration/completed-actions.png) -->

## 12. View migration history

Open the **History** tab to see recent jobs for target instances visible to you. The list shows the
source, target, status, creation time, and actor. Select **View report** to inspect a previous job.

## 13. Troubleshooting

| Symptom | What to check |
|---|---|
| A source or target is missing | Confirm that your access mappings make both instances visible. |
| Planning returns 403 | You need Editor access and Admin access for the target instance. |
| Connection test passes but execution gets 403 | The preflight does not probe write permissions; check the target token's NetBox permissions. |
| A required box cannot be cleared | Another directly selected type requires it. Clear the dependent type first. |
| More types appear in the plan than you checked | Required and referenced optional dependencies are resolved by the backend. |
| The Run button is disabled | Re-plan pending mapping changes, resolve all ambiguous matches, and select the review confirmation. |
| Objects unexpectedly map instead of being created | Review their natural keys and use a manual **Create new** override if a distinct target object is intended. |
| A retry fails again | Open the detailed report and correct the target validation, permissions, relationship, or connectivity issue first. |
| Rollback leaves objects or values behind | Rollback deletes only objects created by the job and is best-effort; mapped/updated objects and some patches are retained. |

