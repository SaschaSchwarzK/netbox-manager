---
sidebar_position: 4
sidebar_label: Tenant permissions
title: Tenant permission automation
description: Templates, onboarding, fleet updates, and safe decommissioning of tenant-scoped NetBox permissions.
---

# Tenant permission automation

> [!NOTE]
> Tenant Permissions are retained for compatibility with existing tenant-scoped deployments.
> For new deployments, prefer [Owner Permission Automation](owner-permissions.md) when NetBox
> ownership can express the required access boundary. Owner Permissions reuse one generic RO/RW
> policy per instance instead of creating groups and permission records per tenant. This reduces
> permission proliferation and can reduce NetBox database work by avoiding repeated tenant-specific
> permission evaluation and, for direct-owner models, longer relationship traversals.

NetBox Manager can create and maintain a read-only and a read-write NetBox group for each tenant.
Reusable permission templates define which NetBox object types those groups may access. Wherever
NetBox has a reliable relationship to the tenant, the generated object permissions are constrained
to that tenant's immutable numeric ID.

This feature manages **NetBox groups and object permissions only**. It does not add users to groups
or change identity-provider membership. Your existing NetBox OIDC or SSO automation must map users
to the exact group names entered during onboarding.

## How the pieces fit together

1. A permission template in GitHub lists object types, actions, and the approved path from each
   object to its tenant.
2. Onboarding selects a NetBox instance and tenant, an RO group, an RW group, and one or two
   templates.
3. NetBox Manager creates or reconciles the groups and their object permissions in NetBox.
4. It opens a GitHub pull request containing resolved permissions and tracking metadata.
5. After that pull request is merged, the tenant appears as managed and can receive later
   fleet-wide template updates.

GitHub stores the policy and generated state; NetBox contains the effective groups and object
permissions. The local SQLite database does not hold tenant permission templates.

## Requirements and authorization

Before onboarding a tenant:

- Configure a visible GitHub target whose token can read repository content, create branches, and
  open pull requests.
- Configure a visible NetBox 4.6.8 or newer instance with an API token allowed to read tenants and
  create, update, and delete groups and object permissions.
- Create or merge at least one valid permission template into the GitHub target's base branch.
- Configure NetBox SSO so the intended identity-provider groups map to corresponding NetBox groups.
- Grant the operator at least the NetBox Manager `editor` role and the `admin` role on every
  affected NetBox instance. Application administrators also satisfy this requirement.

Template listing is read-only. Template validation and saving require `editor`. Onboarding,
fleet planning/apply, and decommissioning additionally require `admin` on every affected instance.

## Permission templates

Tenant permission templates have their own namespace. Built-in templates live under
`templates/tenant-permissions/`; custom templates created in the UI are stored under:

```text
templates/tenant-permissions/custom/<template-name>.yaml
```

Repositories using the former `templates/<template-name>.yaml` or
`templates/custom/<template-name>.yaml` paths remain readable for compatibility. New and edited
custom templates are always written to the namespaced path.

A template has this structure:

```yaml
name: tenant-standard
version: 1
description: Standard permissions for managed tenants
permissions:
  - object_type: dcim.device
    tenant_relation: tenant
    actions: [view, add, change, delete]

  - object_type: dcim.interface
    tenant_relation: device__tenant
    actions: [view, add, change]

  - object_type: virtualization.virtualmachine
    tenant_relation: tenant
    actions: [view]

  - object_type: ipam.fhrpgroup
    tenant_relation: unsupported
    actions: []
```

### Template fields

| Field | Meaning |
|---|---|
| `name` | Lowercase template identifier, up to 64 characters; letters, digits, `.`, `_`, and `-` are accepted |
| `version` | String or integer recorded in managed-tenant metadata |
| `description` | Human-readable purpose of the template |
| `object_type` | NetBox object type in `<app_label>.<model>` form |
| `tenant_relation` | Approved Django-style path from that object to its tenant |
| `actions` | Any of `view`, `add`, `change`, and `delete` |
| `accept_unscoped` | Explicitly allow actions without tenant constraints when no reliable relation exists |
| `note` | Required justification when `accept_unscoped: true` is used |
| `no_auto_view` | Prevent automatic addition of `view` when another action is granted |

Validation is fail-closed. Every object type must be present in NetBox Manager's canonical
`tenant-relations.yaml`, the supplied `tenant_relation` must match it exactly, and blocklisted
object types are rejected. Duplicate object types and unknown actions are also rejected.

Unless `no_auto_view: true` is set, validation automatically adds `view` when `add`, `change`, or
`delete` is requested. The UI rewrites the editor with this normalized result after validation.

### Tenant constraints

For a selected tenant with ID `42`, these relations become NetBox object-permission constraints:

| Template relation | Generated constraint |
|---|---|
| `tenant` | `tenant__id: 42` |
| `device__tenant` | `device__tenant__id: 42` |
| `virtual_machine__tenant` | `virtual_machine__tenant__id: 42` |

The registry contains direct and reviewed indirect paths for supported NetBox models. It is the
source of truth; templates cannot invent a different path.

### Unsupported and unscoped grants

Some global objects or generic relationships cannot be constrained reliably to one tenant. Their
registry value is `unsupported`. The safe default is an empty `actions` list.

Granting actions for such an object requires both:

```yaml
tenant_relation: unsupported
accept_unscoped: true
note: Required business justification for access across every tenant
```

This produces an object permission with empty constraints, so it applies across all tenants. The
editor, plan, and apply result highlight these grants. Treat every warning as a security review,
not as informational noise.

## Create or update a template

1. Open **Tenant Permissions** and select the GitHub target.
2. Open **Templates** and select **New custom template**, or edit an existing custom template.
3. Enter the YAML and select **Validate**.
4. Review normalization and all unscoped-grant warnings.
5. Select **Validate, save, and open PR**.
6. Review and merge the pull request before using the new version.

Saving never commits directly to the configured base branch. Renaming a custom template creates
the new file and removes its previous custom-template path in the same pull request.

## Onboard a tenant

1. Open **Onboard tenant** and select a NetBox instance.
2. Search NetBox for the tenant and select it by name and numeric ID.
3. Enter distinct, exact RO and RW OIDC group names.
4. Select the RW permission template.
5. Optionally select a separate RO template. If omitted, NetBox Manager derives one from the RW
   template by retaining only `view` for entries that grant any actions.
6. Select **Reconcile and open PR**.
7. Review the NetBox result and merge the generated-state pull request.

Before changing NetBox, the operation validates all templates and checks for group-name
collisions. If either group already exists but is not tracked for this tenant, onboarding stops
instead of taking ownership of it. RO and RW names must differ.

Reconciliation creates or updates desired permissions before deleting obsolete managed
permissions, avoiding an intentionally permissionless transition. It manages only permissions
whose generated names belong to this tenant and role.

The NetBox changes occur before the tracking pull request is created. If the GitHub operation
fails, inspect NetBox and retry onboarding after correcting repository access. The reconciliation
is designed to be idempotent: rerunning an unchanged configuration produces no permission changes.

## Generated repository state

Onboarding writes these files through a pull request:

```text
tenant-permissions/instances/<instance-name>/tenants/<tenant-id>/metadata.yaml
tenant-permissions/instances/<instance-name>/tenants/<tenant-id>/ro.resolved.yaml
tenant-permissions/instances/<instance-name>/tenants/<tenant-id>/rw.resolved.yaml
```

This namespace keeps Tenant Permissions state separate from Owner Permissions and other generated
instance data. Installations upgrading from the legacy
`instances/<instance-name>/tenants/<tenant-id>/` layout remain readable. The next successful
reconcile writes the new path and removes the legacy files in the same pull request.

`metadata.yaml` records the immutable tenant ID, display name, selected templates and versions,
RO/RW group names, last-apply time, and requesting actor. The resolved files show the exact
permissions and constraints rendered for each group, including warnings for unscoped grants.

NetBox Manager discovers managed tenants from metadata merged into the configured base branch.
An open pull request alone does not make a tenant available for fleet apply or decommissioning.

## Apply a template fleet-wide

Use fleet apply after merging an updated template:

1. Open **Templates** and select **Plan fleet apply** beside the template.
2. NetBox Manager finds visible managed tenants whose RO or RW metadata references that template.
3. Review the per-instance create, update, and delete counts and every unscoped grant.
4. Application is disabled if any tenant plan reports an error.
5. Select **Confirm fleet apply** only after reviewing every row.

Planning reads NetBox and GitHub but performs no mutation. Apply reconciles each matching tenant
independently and reports success or failure per instance. All selected instances are authorized
before planning or applying begins. Successful changes update generated state through pull
requests; failures are retained in the audit log.

Only templates and managed-tenant metadata present on the GitHub target's configured base branch
drive fleet operations. Content that exists only on a pull-request branch is not applied.

## Decommission a tenant

Open **Managed tenants** and select **Decommission**. NetBox Manager first checks whether either
managed NetBox group still has members. The safe operation refuses to continue when members are
present and identifies the affected groups. Remove or migrate those memberships first.

If you explicitly confirm forced deletion, NetBox Manager:

- Removes the tenant's RO and RW NetBox groups.
- Deletes permissions owned solely by those groups and this managed tenant.
- Detaches the groups from shared or foreign permissions instead of deleting those permissions.
- Opens a pull request removing the tenant's generated repository directory.

Decommissioning does not delete the NetBox tenant or its infrastructure objects. NetBox cleanup
happens before the repository cleanup PR is opened, so inspect both systems and retry safely if
the GitHub step fails.

## Audit and safety model

- Template saves, NetBox reconciliations, generated-state changes, fleet apply, and decommission
  results are attributed to the signed-in actor in the audit log.
- Group-name collision detection prevents accidental takeover of unmanaged groups.
- Tenant IDs, rather than mutable tenant names, scope permissions and tracking paths.
- Validation rejects unknown relations and blocklisted object types.
- Unscoped grants require an explicit flag and justification and remain visible in resolved state.
- Pull requests provide review for templates and generated repository state, but NetBox mutations
  performed during onboarding, apply, or decommission are not delayed until those PRs merge.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Tenant search returns nothing | Instance visibility, NetBox connectivity, token access to `/api/tenancy/tenants/`, and the search text |
| A template is rejected | Required fields, object-type spelling, canonical `tenant_relation`, blocklist status, duplicate entries, and allowed actions |
| An existing group causes onboarding to stop | Use a new name or verify why the existing group is not already tracked for this tenant; NetBox Manager will not adopt it implicitly |
| Users do not receive the expected access | NetBox SSO group mapping and exact RO/RW names; this feature does not manage membership |
| Plan reports an unscoped grant | Inspect the template's `accept_unscoped` entry and justification before applying |
| A managed tenant is missing | Merge its generated-state PR into the target's configured base branch |
| Fleet apply matches zero tenants | Confirm merged metadata references that exact template name as RW, RO, or derived RO source |
| Decommission refuses to continue | Remove users from both groups, or use force only after confirming the consequences |
| NetBox changed but the PR failed | Correct GitHub credentials/access and rerun the idempotent operation; inspect the audit record first |

