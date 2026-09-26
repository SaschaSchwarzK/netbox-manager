"""One-time RBAC data backfills.

The lightweight schema migrations in app/migrations.py only add missing
columns. Data backfills therefore live here and use marker rows so they are
safe to invoke on every startup.
"""
import logging

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import models


logger = logging.getLogger(__name__)

LEGACY_RBAC_BACKFILL = "legacy_rbac_backfill_v1"


def backfill_access_mappings(engine: Engine) -> None:
    """Copy legacy role and scope grants into the unified access-mapping table once."""
    with Session(engine) as db:
        if db.get(models.DataMigration, LEGACY_RBAC_BACKFILL) is not None:
            return

        n_roles = 0
        n_scopes = 0

        for legacy in db.query(models.RoleMapping).all():
            existing = (
                db.query(models.AccessMapping)
                .filter(
                    models.AccessMapping.oidc_group == legacy.oidc_group,
                    models.AccessMapping.resource_type == models.SCOPE_ALL,
                    models.AccessMapping.resource_id == models.SCOPE_ALL,
                )
                .first()
            )
            if existing is not None:
                continue
            db.add(
                models.AccessMapping(
                    oidc_group=legacy.oidc_group,
                    role=legacy.role,
                    resource_type=models.SCOPE_ALL,
                    resource_id=models.SCOPE_ALL,
                )
            )
            n_roles += 1

        for legacy in db.query(models.ScopeMapping).all():
            existing = (
                db.query(models.AccessMapping)
                .filter(
                    models.AccessMapping.oidc_group == legacy.oidc_group,
                    models.AccessMapping.resource_type == legacy.resource_type,
                    models.AccessMapping.resource_id == legacy.resource_id,
                )
                .first()
            )
            if existing is not None:
                continue
            db.add(
                models.AccessMapping(
                    oidc_group=legacy.oidc_group,
                    role=None,
                    resource_type=legacy.resource_type,
                    resource_id=legacy.resource_id,
                )
            )
            n_scopes += 1

        db.add(models.DataMigration(name=LEGACY_RBAC_BACKFILL))
        db.commit()

    logger.info(
        "RBAC: backfilled %d role mappings and %d scope mappings into access_mappings",
        n_roles,
        n_scopes,
    )
