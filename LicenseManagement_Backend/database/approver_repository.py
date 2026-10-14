"""Repository helpers for the sequential-approval approver configuration.

The two-stage workflow uses two dedicated configuration tables:

* ``tower_head_approvers`` (Stage 1 - Tower Head)
* ``license_provisioning_approvers`` (Stage 2 - IT / License Provisioning)

This module centralizes read/upsert logic so both the migration seeder and the
API share a single, idempotent implementation. The ``ensure_default_approvers``
function is safe to run on every startup/migration: it never duplicates rows and
never deactivates an existing active approver for the same email.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from database.models import (
    LicenseProvisioningApprover,
    TowerHeadApprover,
)

# Initial approvers for the two-stage workflow.
DEFAULT_TOWER_HEAD = {
    "name": "Akshayaprasanth",
    "email": "akshayap@clouddestinations.com",
    "role": "Tower Head",
}
DEFAULT_PROVISIONING_APPROVER = {
    "name": "Jerry Joel",
    "email": "jerryj@clouddestinations.com",
    "role": "IT / License Provisioning",
}


def _upsert_active(
    db: Session,
    model,
    defaults: dict,
) -> tuple[object, bool]:
    """Insert or reactivate the active approver row for ``defaults['email']``.

    Returns ``(row, created)``. Idempotent: repeated calls update the name/role
    of the existing row for the same email and keep it active, rather than
    inserting a duplicate (the email column is unique).
    """
    email = defaults["email"].strip().lower()
    row = (
        db.query(model)
        .filter(model.email == email)
        .first()
    )
    created = False
    if row is None:
        row = model(
            name=defaults["name"],
            email=email,
            role=defaults["role"],
            is_active=True,
        )
        db.add(row)
        created = True
    else:
        row.name = defaults["name"]
        row.role = defaults["role"]
        row.is_active = True
    return row, created


def ensure_default_approvers(db: Session) -> dict:
    """Ensure the initial Tower Head and provisioning approvers exist.

    Safe to call repeatedly. Returns a small summary describing whether each
    record was created or already present.
    """
    tower_head, th_created = _upsert_active(db, TowerHeadApprover, DEFAULT_TOWER_HEAD)
    provisioning, pr_created = _upsert_active(
        db, LicenseProvisioningApprover, DEFAULT_PROVISIONING_APPROVER
    )
    db.flush()
    return {
        "tower_head": {"id": tower_head.id, "email": tower_head.email, "created": th_created},
        "provisioning": {
            "id": provisioning.id,
            "email": provisioning.email,
            "created": pr_created,
        },
    }


def get_active_tower_head(db: Session) -> TowerHeadApprover | None:
    return (
        db.query(TowerHeadApprover)
        .filter(TowerHeadApprover.is_active.is_(True))
        .order_by(TowerHeadApprover.id)
        .first()
    )


def get_active_provisioning_approver(db: Session) -> LicenseProvisioningApprover | None:
    return (
        db.query(LicenseProvisioningApprover)
        .filter(LicenseProvisioningApprover.is_active.is_(True))
        .order_by(LicenseProvisioningApprover.id)
        .first()
    )
