---
sidebar_position: 3
sidebar_label: Owner permissions
title: Owner permission automation
description: Shared owner-based NetBox permissions, templates, fleet reconciliation, and safe decommissioning.
---

# Owner permission automation

Owner Permissions are the recommended way to grant users access to the NetBox resources they own.
NetBox Manager maintains one shared read-only group and one shared read-write group per NetBox
instance. Reusable templates define the allowed object types and actions, while NetBox evaluates
the current user's membership in each resource's Owner.

Unlike Tenant Permissions, the generated policy is not duplicated for every tenant. The same two
groups and generic Object Permissions serve all owners on an instance. This reduces the number of
groups and permission records NetBox must load and evaluate. Direct owner paths also avoid longer
tenant relationship traversals where the model supports them, reducing database joins during
permission filtering. Tenant Permissions remain available for compatibility with existing
deployments, but new deployments should prefer Owner Permissions when their NetBox ownership model
can express the required access boundaries.

This feature manages **NetBox groups and Object Permissions only**. It never changes Owner
membership, NetBox group membership, or identity-provider mappings. Those relationships remain
under NetBox and OIDC/SSO administration.

## How the pieces fit together

1. NetBox administrators create Owners and assign NetBox users to each Owner.
2. Resources receive an Owner in NetBox, either directly or through an explicitly supported parent.
3. An owner template in GitHub lists object types, actions, and the approved lookup path to
   `users.Owner.users`.
4. Reconcile creates or updates one shared RO group and one shared RW group on the instance.
5. Every constraint retains the literal `"$user"`; NetBox replaces it with the authenticated
   NetBox user's primary key when filtering a queryset.
6. NetBox Manager opens a pull request containing resolved permissions and tracking metadata.

GitHub stores policy and generated state. NetBox contains the effective groups, permissions,
Owners, and user assignments. The local SQLite database does not hold owner templates.

## Requirements and authorization

Before reconciling Owner Permissions:

- Use NetBox 4.6.8 or newer with resource ownership available.
- Create the required Owners and assign users to them in NetBox.
- Assign an Owner to every resource that should be accessible. Ownership is not inferred from a
  tenant, OIDC group, or local NetBox Manager identity.
- Configure a GitHub target whose token can read repository content, create branches, and open
  pull requests.
- Configure a NetBox API token that can create, update, and delete groups and Object Permissions.
- Merge at least one valid owner template into the GitHub target's base branch.
- Grant the operator at least the NetBox Manager `editor` role and `admin` on each affected NetBox
  instance.

Template listing is read-only. Saving and validation require `editor`. Reconcile, fleet planning,
fleet apply, and decommission additionally require `admin` on every affected instance.

## Group model

Each managed instance has exactly two shared groups:

- The RO group receives the read-only template, or a view-only policy derived from the RW template.
- The RW group receives the complete read-write template.

These are ordinary NetBox groups, but their membership is outside this feature. A deployment can
map broad OIDC groups such as `netbox-owner-ro` and `netbox-owner-rw` to them. The resource-level
Owner relationship determines which records an individual group member can access.

Because groups are shared per instance, adding another Owner does not create another pair of groups
or another copy of every Object Permission. This is the principal scaling advantage over
tenant-specific permission groups.

## Permission templates

Owner permission templates have their own namespace. Built-in templates live under
`templates/owner-permissions/`; custom templates created in the UI are stored under:

```text
templates/owner-permissions/custom/<template-name>.yaml
```

Repositories using the former `owner-templates/<template-name>.yaml` or
`owner-templates/custom/<template-name>.yaml` paths remain readable for compatibility. New and
edited custom templates are always written to the namespaced path.

Example:

```yaml
name: device-owners
version: 1
description: Let owners manage devices and interfaces belonging to their devices
permissions:
  - object_type: dcim.device
    owner_relation: owner__users
    actions: [view, add, change, delete]

  - object_type: dcim.interface
    owner_relation: device__owner__users
    actions: [view, add, change, delete]
```

### Template fields

| Field | Meaning |
|---|---|
| `name` | Lowercase template identifier using letters, digits, `.`, `_`, and `-` |
| `version` | String or integer recorded in managed-instance metadata |
| `description` | Human-readable purpose of the template |
| `object_type` | NetBox object type in `<app_label>.<model>` form |
| `owner_relation` | Exact approved lookup from that object to `users.Owner.users` |
| `actions` | Any of `view`, `add`, `change`, and `delete` |
| `no_auto_view` | Prevent automatic addition of `view` when another action is granted |

Validation is fail-closed. Every object type/path pair must exactly match
`config/owner-relations.yaml`; unknown types, arbitrary paths, blocklisted object types, duplicate
entries, and unknown actions are rejected. Owner templates cannot request an unscoped grant.

Unless `no_auto_view: true` is set, validation adds `view` when `add`, `change`, or `delete` is
requested. The UI replaces the editor content with the normalized policy after validation.

## Constraint and `$user` semantics

For the complete constraint model, supported relationship paths, inheritance behavior, and
version-specific limitations, see [Owner permission constraints](owner-constraints.md).

The renderer copies the approved path exactly and always uses the literal string `"$user"`:

| Object type | Generated constraint |
|---|---|
| `dcim.device` | `{"owner__users": "$user"}` |
| `dcim.interface` | `{"device__owner__users": "$user"}` |

NetBox Manager does not replace the token with an OIDC subject, local actor ID, API-token identity,
or NetBox user ID. During permission evaluation, NetBox replaces `"$user"` with the primary key of
the authenticated NetBox user and applies the resulting Django queryset filter.

Owner relationships are explicit per object type. A Device permission does not automatically
grant Interface permission. The Interface entry must declare the complete
`device__owner__users` traversal. Conversely, an Interface's own Owner is not consulted when the
approved policy deliberately scopes it through its Device.

Permissions are combined only when actions and constraints are identical. Direct-owner and
parent-owner grants therefore remain separate even if they allow the same actions.

## Verified paths and compatibility

The registry was source-reviewed against NetBox 4.6.8 and 4.7.0. It currently permits:

| Object types | Approved path |
|---|---|
| `dcim.device` | `owner__users` |
| `dcim.interface` | `device__owner__users` |
| `dcim.consoleport`, `dcim.consoleserverport` | `device__owner__users` |
| `dcim.powerport`, `dcim.poweroutlet` | `device__owner__users` |
| `dcim.frontport`, `dcim.rearport` | `device__owner__users` |
| `dcim.module`, `dcim.modulebay`, `dcim.inventoryitem` | `device__owner__users` |

Other models remain unavailable until their intended ownership semantics and complete lookup path
are verified. An `owner` field in OpenAPI alone is not sufficient evidence that a permission path
is safe.

Validate the registry inside each deployed NetBox environment:

```shell
/opt/netbox/venv/bin/python scripts/check_owner_relations.py \
  --registry config/owner-relations.yaml
```

The script resolves every complete lookup through Django model metadata and verifies that it ends
at the `users.User` many-to-many relationship.

## Create or update a template

1. Open **Owner Permissions** and select the GitHub target.
2. Open **Templates** and select **New owner template**, or edit an existing template.
3. Enter the YAML and select **Validate**.
4. Review the normalized actions and exact relation paths.
5. Select **Validate, save, and open PR**.
6. Review and merge the pull request before reconciliation or fleet apply.

Saving never commits directly to the target's base branch. Owner templates and Tenant templates
use separate repository directories and cannot overwrite one another.

## Reconcile an instance

1. Open **Owner Permissions** and select **Reconcile**.
2. Select the NetBox instance.
3. Enter distinct RO and RW group names.
4. Select the RW template.
5. Optionally select a separate RO template. Otherwise NetBox Manager derives a view-only policy.
6. Select **Reconcile and open PR**.
7. Review the NetBox changes and merge the generated-state pull request.

Before mutation, NetBox Manager validates both templates and checks group-name ownership. It will
not adopt an existing group unless that name is already tracked for Owner Permissions on the same
instance. Reconciliation creates and updates desired permissions before removing obsolete managed
permissions and never modifies foreign permissions or group membership.

## Generated repository state

Reconcile writes these files through a pull request:

```text
owner-permissions/instances/<instance-name>/metadata.yaml
owner-permissions/instances/<instance-name>/ro.resolved.yaml
owner-permissions/instances/<instance-name>/rw.resolved.yaml
```

Metadata records template names and versions, group names, the last-apply time, and the requesting
actor. Resolved files contain the exact Object Permissions sent to NetBox, including the unchanged
`"$user"` token. This path is deliberately separate from Tenant Permissions state.

NetBox Manager discovers managed instances only from metadata merged into the configured base
branch. An open pull request alone does not make an instance available for fleet operations.

## Plan and apply fleet-wide

After merging a template update:

1. Select **Plan fleet apply** beside the template.
2. Review every matched instance and its create, update, and delete counts.
3. Resolve any failed plan before continuing.
4. Select **Confirm fleet apply**.

Planning reads NetBox and GitHub without mutation. Apply reconciles every matched visible instance
independently and records results in the audit log. All selected instances are authorized before
planning or mutation begins.

## Decommission Owner Permissions

Open **Managed instances** and select **Decommission**. NetBox Manager refuses to delete either
managed group while it has members unless an operator explicitly confirms forced deletion.

Decommissioning:

- Removes the managed RO and RW groups.
- Deletes Object Permissions owned solely by those groups and the Owner feature.
- Detaches the groups from shared or foreign permissions rather than deleting those permissions.
- Opens a pull request removing only that instance's Owner Permissions tracking directory.

It does not remove Owners, resource ownership assignments, users, OIDC mappings, or infrastructure
objects.

## Audit and safety model

- All template saves, reconciliations, fleet operations, and decommissions are attributed to the
  signed-in actor.
- Exact registry matching prevents arbitrary constraint injection.
- Empty constraints and unscoped Owner grants are never generated.
- The `"$user"` token is preserved for NetBox to evaluate.
- Group collisions prevent accidental adoption of unmanaged groups.
- Stable permission-name prefixes ensure only Owner-managed permissions are reconciled or removed.
- Owner and Tenant templates, metadata, permission names, and API routes remain separate.

## Troubleshooting

| Symptom | What to check |
|---|---|
| A template is rejected | Exact `object_type` and `owner_relation`, registry support, blocklist status, duplicates, and allowed actions |
| A user sees no owned objects | The user exists in NetBox, belongs to the resource's Owner, belongs to the shared RO/RW group, and the resource has the expected Owner |
| A Device is visible but its Interface is not | The template needs a separate `dcim.interface` entry using `device__owner__users` |
| Existing group blocks reconcile | Use another name or verify it is already tracked for this instance; unmanaged groups are never adopted implicitly |
| Managed instance is missing | Merge its generated-state PR into the configured base branch |
| Fleet apply matches nothing | Confirm merged metadata references the exact template name |
| Decommission is refused | Remove group members or use force only after reviewing the consequences |
| NetBox changed but the PR failed | Correct GitHub access and rerun the idempotent reconcile operation |
