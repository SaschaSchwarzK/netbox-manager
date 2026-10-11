---
sidebar_position: 1
sidebar_label: Quick start
title: Quick start
description: Minimum requirements and first deployment of NetBox Manager.
---

# NetBox Manager quick start

This guide starts NetBox Manager as a single hardened container and connects it to GitHub and
NetBox. NetBox Manager is under active development: back up each NetBox instance and test changes
outside production before relying on it.

## Minimum requirements

| Requirement | Minimum |
|---|---|
| Container host | Linux with Docker Engine or another OCI-compatible runtime |
| Architecture | `linux/amd64` or `linux/arm64` |
| Compute | 1 CPU and 256 MiB RAM available to the container |
| NetBox | NetBox 4.6.8 or newer, reachable over HTTPS |
| Storage | Three writable persistent volumes for the database, TLS certificates, and exports; keep at least 200 MiB free in the export volume |
| Network | Outbound HTTPS access to the configured NetBox instances, GitHub, and your OIDC provider; inbound TCP 8443 for the web UI |
| GitHub | A repository for managed YAML and a token allowed to read contents and create or update branches and pull requests |
| Authentication | An OIDC client or a break-glass local administrator account |

A current web browser is required. More CPU, memory, and export storage are recommended for large
fleets, concurrent migrations, or large CSV and Excel exports.

## 1. Download and configure the environment

Download the example configuration:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/SaschaSchwarzK/netbox-manager/main/.env.example \
  -o .env
```

Generate the encryption key used for stored NetBox and GitHub credentials:

```bash
docker run --rm \
  --entrypoint /app/venv/bin/python \
  ghcr.io/saschaschwarzk/netbox-manager:latest \
  -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set the generated value as `NBM_SECRET_KEY` in `.env`. Then configure either OIDC or both
`NBM_LOCAL_ADMIN_USER` and `NBM_LOCAL_ADMIN_PASSWORD`. See [Configure SSO and OIDC](oidc-sso.md)
for provider registration, group claims, and application permission mappings. Keep `.env` private
and never commit it.

## 2. Create persistent storage

```bash
docker volume create netbox-manager_dbdata
docker volume create netbox-manager_certdata
docker volume create netbox-manager_exportdata
```

## 3. Start NetBox Manager

```bash
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

Open `https://HOSTNAME:8443`. The first start creates a self-signed TLS certificate, so your
browser will warn until you install a certificate trusted by your environment.

Check that the container is healthy:

```bash
docker ps --filter name=netbox-manager
docker logs netbox-manager
```

## 4. Complete the initial setup

1. Sign in with OIDC or the configured local administrator.
2. Open **NetBox Instances**, add an instance and API token, and test the connection.
3. Open **GitHub Targets**, configure the YAML repository and token, and test the connection.
4. Open **Access Control** and map identity-provider groups to the required roles and resources.
5. Import or create a template, review the generated pull request, and test publishing against a
   non-production NetBox instance.

Continue with the [User Guide](user-guide.md) for daily workflows. The project
[README](../README.md) contains complete configuration, backup, certificate, and upgrade details.

