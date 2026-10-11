---
sidebar_position: 2
sidebar_label: SSO and OIDC
title: Configure SSO and OIDC
description: Configure an OpenID Connect provider, group claims, and NetBox Manager access mappings.
---

# Configure SSO and OIDC

NetBox Manager supports the OpenID Connect authorization-code flow for browser sign-in. The
identity provider authenticates the user and supplies identity and group claims; NetBox Manager
maps those group values to application roles and resource visibility.

## Before you begin

You need:

- A public HTTPS URL for NetBox Manager, such as `https://netbox-manager.example.com:8443`.
- Permission to register a confidential web application with your identity provider.
- At least one identity-provider group that will permanently administer NetBox Manager.
- A client ID and client secret.

Keep a break-glass local administrator configured while setting up SSO. It remains available if
the provider is unreachable or a claim mapping is accidentally changed.

## 1. Register the OIDC client

Create a confidential web application/client in the identity provider and enable the authorization
code flow. Register this exact redirect URI:

```text
https://netbox-manager.example.com:8443/api/auth/callback
```

Replace the hostname and port with the address users actually open. Scheme, host, port, path, and
trailing slash must match the registered URI exactly. Do not register a wildcard callback.

The provider must expose standard OIDC discovery metadata at:

```text
ISSUER/.well-known/openid-configuration
```

The verified identity data must contain `sub`. The application also reads `name`,
`preferred_username`, and `email` for display and auditing when available.

## 2. Include a group claim

Configure the provider to put the user's relevant group memberships in the verified OIDC identity
claims. The default claim name is `groups`, and its value must be either one string or an array of
strings:

```json
{
  "sub": "00u123example",
  "name": "Network Operator",
  "email": "operator@example.com",
  "groups": ["netbox-viewers", "netbox-editors"]
}
```

Group matching is exact and case-sensitive. Map precisely the value emitted in the claim—not
necessarily the friendly group name shown in the provider's administration interface. NetBox
Manager accepts at most 500 groups per login and truncates individual values to 256 characters.
It does not query a provider directory or Microsoft Graph for missing memberships.

Common provider considerations:

- **Keycloak:** add a Group Membership protocol mapper, set the token claim name to `groups`, and
  include it in the ID token. Decide whether to emit full paths such as `/network/netbox-admins` or
  short names, then use that exact format in NetBox Manager.
- **Microsoft Entra ID:** group claims normally contain group object IDs, so access mappings should
  use those IDs. Limit the claim to groups assigned to the application where possible. If a JWT
  exceeds Entra's group limit, Entra emits an overage indicator instead of the group list; NetBox
  Manager does not currently follow that indicator to Microsoft Graph.
- **Okta:** define a groups claim on the authorization server, include it in the ID token, and use a
  filter that returns only the groups required by NetBox Manager.

If your provider uses another claim name, set `NBM_OIDC_GROUPS_CLAIM` to that name. Nested claim
paths are not interpreted; configure a top-level string or array claim.

## 3. Configure NetBox Manager

Add the following values to `.env`:

```dotenv
AUTHENTICATION_DISABLED=False

NBM_OIDC_ISSUER=https://idp.example.com/realms/example
NBM_OIDC_CLIENT_ID=netbox-manager
NBM_OIDC_CLIENT_SECRET=replace-with-the-client-secret
NBM_OIDC_REDIRECT_URI=https://netbox-manager.example.com:8443/api/auth/callback
NBM_OIDC_SCOPES=openid profile email groups
NBM_OIDC_GROUPS_CLAIM=groups

NBM_BOOTSTRAP_ADMIN_GROUPS=netbox-admins
NBM_DEFAULT_ROLE=viewer
NBM_SESSION_COOKIE_SECURE=true
NBM_SESSION_SECRET_KEY=replace-with-a-random-value-of-at-least-32-characters

# Recommended recovery account
NBM_LOCAL_ADMIN_USER=break-glass-admin
NBM_LOCAL_ADMIN_PASSWORD=replace-with-a-long-random-password
```

Generate a suitable session secret with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

`NBM_OIDC_ISSUER` is the issuer base URL, not the discovery-document URL. All three of issuer,
client ID, and client secret must be present or NetBox Manager refuses to start. Restart the
container after changing `.env`.

`NBM_BOOTSTRAP_ADMIN_GROUPS` is a comma-separated list of exact group-claim values. A member of any
listed group is an application administrator and can create the first access mappings. Keep at
least one permanent bootstrap group; do not treat it as a one-time installation setting.

## 4. Verify the first login

1. Open NetBox Manager in a private browser window and select **Sign in with SSO**.
2. Complete authentication at the provider.
3. Confirm that the sidebar shows the expected user name and groups.
4. Confirm that a bootstrap administrator can open **Access Control**.

NetBox Manager validates the ID token signature using the provider's JWKS as well as its issuer,
audience, expiry, and nonce. The browser receives a random HttpOnly session identifier; verified
identity claims are stored in the local SQLite database. Group changes take effect after the user
signs out and signs in again, or after an administrator revokes that user's sessions.

## 5. Map groups to permissions

Open **Access Control** as an application administrator. Groups observed during successful logins
appear as suggestions, or you can enter an exact claim value manually.

Each mapping combines a group, role, and scope:

| Role | Capability |
|---|---|
| `viewer` | Inspect visible resources, templates, fleet state, drift, searches, and audit data |
| `editor` | Viewer access plus ordinary template edits, imports, previews, and pushes |
| `admin` | Administer the selected resource; on **All resources**, also administer the application |
| Use default role | Visibility-only mapping for a concrete resource |

| Scope | Effect |
|---|---|
| **All resources** | Grants the role globally, but does not override concrete visibility restrictions |
| **NetBox instance** | Grants visibility and the selected role on one instance |
| **GitHub target** | Grants visibility and the selected role on one repository target |

The highest matching role is used. `NBM_DEFAULT_ROLE` is the fallback for authenticated users with
no mapped role. Resource visibility is opt-in: a resource with no concrete mappings is visible to
all authenticated users, while adding its first concrete mapping restricts it to groups mapped to
that resource. A global role alone does not reveal a concretely scoped resource.

Example:

| Group | Role | Scope |
|---|---|---|
| `netbox-viewers` | viewer | All resources |
| `netbox-eu-editors` | editor | NetBox instance: Europe |
| `netbox-library-admins` | admin | GitHub target: Device Type Library |

In this example, EU editors can modify the Europe instance only if their group can also see it.
The library administrators can administer that GitHub target but are not application administrators.

## Troubleshooting

| Symptom | Check |
|---|---|
| **Sign in with SSO** is unavailable | All of `NBM_OIDC_ISSUER`, `NBM_OIDC_CLIENT_ID`, and `NBM_OIDC_CLIENT_SECRET` must be set |
| Provider reports a redirect mismatch | Compare the provider URI with `NBM_OIDC_REDIRECT_URI` character for character |
| Login returns `OIDC login failed` | Verify issuer discovery, client secret, callback URI, TLS trust, and container access to the provider |
| Login works but no groups appear | Ensure the configured claim is included in the ID token as a string or array and matches `NBM_OIDC_GROUPS_CLAIM` |
| Entra user unexpectedly has no groups | Check for a group-overage claim and restrict emitted groups to those assigned to the application |
| User has the default role only | Compare the displayed group value with the access mapping, including case and any Keycloak path prefix |
| User cannot see one resource | Add a concrete visibility/role mapping for one of the user's groups |
| Nobody can manage access mappings | Sign in with the local recovery account or restore a valid `NBM_BOOTSTRAP_ADMIN_GROUPS` value and restart |
| Group membership changed but access did not | Sign out and in again, or revoke the user's active sessions |

