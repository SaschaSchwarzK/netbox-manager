# Tenant Permissions Guide

The **Tenant Permissions** feature manages tenant-specific NetBox groups and object permissions
from versioned YAML templates. It can onboard a tenant, reconcile template changes across managed
tenants, and decommission the groups and permissions it owns.

The feature uses a GitHub pull-request workflow for templates and generated state. It does not add
users to groups. Your existing OIDC login automation remains responsible for matching users to the
read-only and read-write groups.

## 1. How the feature works

For each managed tenant, NetBox Manager:

1. Reads a merged permission template from the selected GitHub target's base branch.
2. Resolves the template for a specific NetBox tenant ID.
3. Creates or reconciles separate read-only and read-write NetBox groups.
4. Creates, updates, or removes only the object permissions managed for those groups.
5. Opens a pull request containing resolved grants and tenant metadata.

Templates and managed-tenant metadata on an open pull-request branch cannot drive a NetBox change.
Merge the pull request into the configured base branch before relying on it for onboarding or fleet
application.

## 2. Before you begin

Make sure that:

- A **GitHub Target** is configured with a repository, base branch, and token that can open pull
  requests.
- The NetBox instance is configured and visible to you.
- The target NetBox token can manage groups and object permissions.
- Your account has at least the **Editor** role and the **Admin** role for every NetBox instance you
  intend to change.
- The required OIDC groups have stable, distinct names.
- The desired tenant already exists in NetBox.

Viewers can list templates and managed tenants visible to them. Creating templates, onboarding,
fleet application, and decommissioning require stronger access; NetBox mutations require Admin
access to each affected instance.

> **Screenshot placeholder:** Tenant Permissions page with the GitHub target selector and tabs.
> Suggested file: `docs/images/tenant-permissions/overview.png`
>
> <!-- Replace this blockquote with: ![Tenant Permissions overview](images/tenant-permissions/overview.png) -->

## 3. Select the GitHub target

Open **Tenant Permissions** and select the GitHub target in the toolbar. Templates and managed
tenant records are repository-specific, so changing the target changes all three tabs.

If no target exists, add one on the **GitHub Targets** page first.

## 4. Understand the permission-template YAML

Open the **Templates** tab and select **+ New custom template**. A template has four required
top-level fields:

```yaml
name: tenant-operator
version: 1
description: Read and change common tenant-owned infrastructure objects.
permissions:
  - object_type: dcim.device
    tenant_relation: tenant
    actions:
      - view
      - change

  - object_type: dcim.interface
    tenant_relation: device__tenant
    actions:
      - view
      - add
      - change

  - object_type: ipam.prefix
    tenant_relation: tenant
    actions:
      - view
      - add
      - change
      - delete
```

### Top-level fields

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Template identifier. Use lowercase letters, digits, dots, underscores, or hyphens; begin with a letter or digit; maximum 64 characters. |
| `version` | Yes | String or integer used to identify the template revision in generated metadata. Increase it when permission intent changes. |
| `description` | Yes | Human-readable purpose of the template. It may be an empty string. |
| `permissions` | Yes | List of object-type permission entries. |

### Permission-entry fields

| Field | Required | Description |
|---|---|---|
| `object_type` | Yes | NetBox content type in `app_label.model` form, for example `dcim.device`. Each type may appear only once. |
| `tenant_relation` | Yes | Canonical path from that model to its tenant, such as `tenant` or `device__tenant`. It must exactly match `config/tenant-relations.yaml`. |
| `actions` | Yes | Any combination of `view`, `add`, `change`, and `delete`; use an empty list to grant nothing for that type. |
| `no_auto_view` | No | Set to `true` only when a write action must not automatically gain `view`. Defaults to `false`. |
| `accept_unscoped` | No | Explicitly accepts a global, unscoped grant for a type whose relation is `unsupported`. Defaults to `false`. |
| `note` | Required for an accepted unscoped grant | Explains why the global grant is necessary and has been accepted. |

When actions contain `add`, `change`, or `delete` but omit `view`, validation adds `view`
automatically unless `no_auto_view: true` is present. Selecting automatic read-only derivation
during onboarding reduces every non-empty permission entry to `view` for the RO group.

### Tenant relations are not free-form

Do not guess `tenant_relation`. NetBox Manager validates every value against
[`config/tenant-relations.yaml`](../config/tenant-relations.yaml). Examples include:

- `dcim.device` → `tenant`
- `dcim.interface` → `device__tenant`
- `dcim.powerpanel` → `site__tenant`
- `virtualization.vminterface` → `virtual_machine__tenant`

Types absent from the registry are rejected. Identity and RBAC types in
[`config/blocklist.yaml`](../config/blocklist.yaml), such as users, groups, tokens, and permission
models, are always rejected.

### Explicitly unscoped grants

Some object types have no safe static path to one tenant. Granting actions for such a type applies
across all tenants and therefore requires an explicit acceptance and note:

```yaml
name: tenant-network-extended
version: 2
description: Tenant network permissions with an explicitly reviewed global FHRP grant.
permissions:
  - object_type: ipam.prefix
    tenant_relation: tenant
    actions: [view, add, change, delete]

  - object_type: ipam.fhrpgroup
    tenant_relation: unsupported
    actions: [view]
    accept_unscoped: true
    note: FHRP groups have no tenant field; network administrators approved global read access.
```

The editor and fleet plan highlight unscoped grants. Treat each warning as a security decision, not
as a validation nuisance. Prefer omitting the object type when global access is not acceptable.

## 5. Validate and save a template

In the template editor:

1. Enter or edit the YAML.
2. Select **Validate**.
3. Review the normalized YAML. Action order is normalized and automatic `view` access is added
   where applicable.
4. Resolve every validation error and review every unscoped warning.
5. Select **Validate, save, and open PR**.
6. Review and merge the pull request in GitHub.

Custom templates are stored as `templates/custom/<name>.yaml`. Renaming a custom template through
the editor adds the new file and removes the old custom-template file in the same pull request.

> **Screenshot placeholder:** YAML editor after successful validation, including normalized actions.
> Suggested file: `docs/images/tenant-permissions/template-editor.png`
>
> <!-- Replace this blockquote with: ![Permission template editor](images/tenant-permissions/template-editor.png) -->

## 6. Onboard a tenant

Open the **Onboard tenant** tab and complete the form:

1. Select the **NetBox instance**.
2. Search for the tenant by name and select the result with the correct numeric ID. At most 50
   matches are shown.
3. Enter the **OIDC read-only group** exactly as supplied by the identity provider.
4. Enter a different **OIDC read-write group** name.
5. Select the **Read-write template**.
6. Either leave **Read-only template** at **Derive automatically from RW**, or select a separate
   template for the RO group.
7. Select **Reconcile and open PR**.

NetBox Manager refuses to take over an existing group with the same name unless that group is
already tracked for this tenant. This collision check prevents accidental modification of an
unrelated group.

The tenant is tracked by its immutable NetBox numeric ID. Its name is retained for display, so a
later tenant rename does not break or silently broaden the grant.

Onboarding changes NetBox immediately and opens a repository pull request for the generated state.
If no effective permission or metadata change is needed, reconciliation is idempotent and does not
open a redundant pull request.

> **Screenshot placeholder:** Completed onboarding form before reconciliation.
> Suggested file: `docs/images/tenant-permissions/onboard-form.png`
>
> <!-- Replace this blockquote with: ![Onboard a tenant](images/tenant-permissions/onboard-form.png) -->

## 7. Review generated repository state

For tenant ID `42` on an instance named `production`, generated files use this layout:

```text
instances/
  production/
    tenants/
      42/
        metadata.yaml
        ro.resolved.yaml
        rw.resolved.yaml
```

`metadata.yaml` records the tenant ID and display name, group names, template names and versions,
and the last application actor and time. The resolved files show the exact constraints and actions
that were reconciled into NetBox.

A typical resolved permission is tenant-specific:

```yaml
object_type: dcim.interface
actions:
  - view
  - change
constraints:
  device__tenant__id: 42
```

Resolved output is generated evidence. Edit the source template through the UI instead of manually
editing a resolved file.

## 8. Apply a template update fleet-wide

After changing and merging a template:

1. Open **Templates**.
2. Find the updated template and select **Plan fleet apply**.
3. Review every matched managed tenant.
4. Check each row's instance, tenant ID, status, create/update/delete totals, and unscoped-grant
   count.
5. Correct any error row. **Confirm fleet apply** stays disabled while a row has an error.
6. Select **Confirm fleet apply** and accept the confirmation dialog.

The fleet is selected from merged tenant metadata that references the chosen template. This
includes a template used directly for RW or RO access and an RW template from which RO access was
derived. NetBox Manager verifies Admin access for every selected instance before plan calls or
mutations proceed.

The plan is read-only. Confirmation reconciles each tenant and opens generated-state pull requests
where needed.

> **Screenshot placeholder:** Fleet apply plan with create, update, delete, and unscoped totals.
> Suggested file: `docs/images/tenant-permissions/fleet-plan.png`
>
> <!-- Replace this blockquote with: ![Tenant permission fleet plan](images/tenant-permissions/fleet-plan.png) -->

## 9. Review managed tenants

Open **Managed tenants** to see repository-tracked tenants visible to you. Each row shows:

- NetBox instance
- Tenant name and immutable numeric ID
- Read-only and read-write OIDC group names
- Applied read-write template and version

If a tenant is missing, verify that its metadata pull request was merged into the selected GitHub
target's base branch and that you can see the corresponding NetBox instance.

## 10. Decommission a tenant

Decommissioning removes the tracked RO and RW groups and the permissions owned for the tenant, then
opens a pull request that removes the generated repository files.

1. Open **Managed tenants**.
2. Find the tenant and select **Decommission**.
3. Confirm the initial prompt.
4. If either group still has members, NetBox Manager refuses the safe attempt and reports the group
   names and member counts.
5. Remove or reassign those memberships and retry. Use the second **Force deletion** confirmation
   only when deleting populated groups is explicitly intended.
6. Review and merge the repository cleanup pull request.

The feature deletes groups and permissions; it does not delete the NetBox tenant itself and does
not change identity-provider group membership.

> **Screenshot placeholder:** Managed tenant row and decommission action.
> Suggested file: `docs/images/tenant-permissions/managed-tenants.png`
>
> <!-- Replace this blockquote with: ![Managed tenants](images/tenant-permissions/managed-tenants.png) -->

## 11. Security and operational rules

- Group names are used verbatim and RO/RW names must differ.
- Existing, untracked NetBox groups are never silently adopted.
- The relation registry and RBAC blocklist are enforced before changes are applied.
- Unsupported relations with actions require `accept_unscoped: true` and a non-empty justification.
- NetBox Manager owns only permissions carrying its stable management marker; reconciliation and
  decommissioning do not intentionally remove unrelated permissions.
- Open pull-request content is not treated as approved source material.
- Onboarding and application are auditable as NetBox and GitHub actions.
- Group membership remains the responsibility of OIDC login automation.

## 12. Troubleshooting

| Symptom | What to check |
|---|---|
| No GitHub target is available | Add and test a GitHub target first. |
| A template is missing after save | Merge its pull request into the target's configured base branch. |
| Validation says the tenant relation is wrong | Copy the exact path for that object type from `config/tenant-relations.yaml`. |
| Validation says the object type is absent | The type is fail-closed; review and update the canonical registry before using it. |
| Validation says the type is blocklisted | The type cannot be granted through tenant templates. Do not bypass the blocklist. |
| An unscoped warning appears | The grant has no tenant constraint. Remove it unless global access was deliberately reviewed. |
| Onboarding reports a group collision | Choose unused group names or verify that you selected the correct already-managed tenant. |
| A tenant search returns no results | Confirm the instance, spelling, visibility, NetBox token access, and the 50-result limit. |
| Fleet apply matches zero tenants | Merge generated metadata and confirm that it references the selected template. |
| Fleet apply is disabled | Resolve every plan row whose status is not `success`. |
| Decommissioning is refused | One or both groups still have members; remove them or explicitly confirm forced deletion. |

## 13. Validate the relation registry against NetBox

Administrators maintaining the policy registry can check direct tenant relationships inside the
target NetBox installation's Django environment:

```sh
/opt/netbox/venv/bin/python scripts/check_tenant_relations.py \
  --registry config/tenant-relations.yaml
```

The checker validates direct `Tenant` foreign keys. Indirect paths such as `device__tenant` and
unsupported relationships still require manual review.

