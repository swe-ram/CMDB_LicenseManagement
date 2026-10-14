"""Apply the additive two-stage sequential approval migration.

This runner mirrors ``apply_license_request_migration.py``:

* It requires PostgreSQL.
* It verifies the existing portal tables are present before changing anything.
* It executes the additive SQL in ``002_sequential_license_approval.sql``.

The migration only ADDS tables/columns, relaxes ``license_approvals.approver_id``
to nullable, backfills the stage column, and idempotently seeds the two initial
approvers. It never drops, truncates, recreates, or resets existing tables and
never deletes existing requests, users, applications, licenses, departments, or
approval records.
"""

import sys
from pathlib import Path

from sqlalchemy import inspect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.database import engine


def main() -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "The sequential approval migration requires PostgreSQL."
        )

    required_tables = {
        "users",
        "departments",
        "applications",
        "licenses",
        "license_requests",
        "license_approvals",
        "department_approvers",
    }
    existing_tables = set(inspect(engine).get_table_names())
    missing_tables = required_tables - existing_tables
    if missing_tables:
        raise RuntimeError(
            "The migration expects the existing portal schema; missing tables: "
            + ", ".join(sorted(missing_tables))
        )

    migration = Path(__file__).with_name(
        "002_sequential_license_approval.sql"
    ).read_text(encoding="utf-8")

    with engine.begin() as connection:
        connection.exec_driver_sql(migration)

    print(
        "Applied the additive two-stage sequential approval migration: added "
        "tower_head_approvers and license_provisioning_approvers, extended "
        "license_approvals with stage/approver snapshot columns, and seeded the "
        "initial Tower Head and IT / License Provisioning approvers. No existing "
        "tables or records were replaced or deleted."
    )


if __name__ == "__main__":
    main()
