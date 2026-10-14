-- =========================================================
-- 002_sequential_license_approval.sql
-- =========================================================
-- Additive migration for the two-stage sequential approval workflow.
--
-- This migration is intentionally additive and safe:
--   * It only ADDS new tables and columns.
--   * It never drops, truncates, recreates, or resets existing tables.
--   * It never deletes existing requests, users, applications, licenses,
--     departments, or approval records.
--   * Approver seeding uses ON CONFLICT so it is idempotent and never creates
--     duplicate approver rows.
--
-- Apply with: python migrations/apply_sequential_license_approval.py

-- ---------------------------------------------------------
-- Table A: Tower Head approver configuration (Stage 1)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS tower_head_approvers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    role VARCHAR(100) NOT NULL DEFAULT 'Tower Head',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tower_head_approvers_email
    ON tower_head_approvers (email);

-- ---------------------------------------------------------
-- Table B: IT / License Provisioning approver config (Stage 2)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS license_provisioning_approvers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    role VARCHAR(100) NOT NULL DEFAULT 'IT / License Provisioning',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_license_provisioning_approvers_email
    ON license_provisioning_approvers (email);

-- ---------------------------------------------------------
-- Extend license_approvals to represent both stages.
-- ---------------------------------------------------------
ALTER TABLE license_approvals
    ADD COLUMN IF NOT EXISTS stage VARCHAR(40);

ALTER TABLE license_approvals
    ADD COLUMN IF NOT EXISTS approver_name VARCHAR(255);

ALTER TABLE license_approvals
    ADD COLUMN IF NOT EXISTS approver_email VARCHAR(255);

ALTER TABLE license_approvals
    ADD COLUMN IF NOT EXISTS tower_head_approver_id INTEGER;

ALTER TABLE license_approvals
    ADD COLUMN IF NOT EXISTS provisioning_approver_id INTEGER;

-- Legacy single-stage rows were NOT NULL on approver_id. Relax it to nullable
-- so the sequential workflow can reference its own approver tables. This does
-- not alter or delete any existing data.
ALTER TABLE license_approvals
    ALTER COLUMN approver_id DROP NOT NULL;

-- Backfill stage for pre-existing rows so history keeps rendering. Existing
-- rows used the historical "Department / Tower Head Approval" stage label.
UPDATE license_approvals
SET stage = 'TOWER_HEAD'
WHERE stage IS NULL;

-- Additive foreign keys (guarded so re-running is safe).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_license_approvals_tower_head_approver'
    ) THEN
        ALTER TABLE license_approvals
            ADD CONSTRAINT fk_license_approvals_tower_head_approver
            FOREIGN KEY (tower_head_approver_id)
            REFERENCES tower_head_approvers (id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_license_approvals_provisioning_approver'
    ) THEN
        ALTER TABLE license_approvals
            ADD CONSTRAINT fk_license_approvals_provisioning_approver
            FOREIGN KEY (provisioning_approver_id)
            REFERENCES license_provisioning_approvers (id);
    END IF;
END
$$;

-- One approval record per request per stage. Guarded because the constraint may
-- already exist. We first ensure there are no duplicate (request_id, stage)
-- rows that would prevent creating it; if duplicates exist we skip rather than
-- delete existing approval history.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_license_approvals_request_stage'
    ) THEN
        IF NOT EXISTS (
            SELECT request_id, stage
            FROM license_approvals
            GROUP BY request_id, stage
            HAVING COUNT(*) > 1
        ) THEN
            ALTER TABLE license_approvals
                ADD CONSTRAINT uq_license_approvals_request_stage
                UNIQUE (request_id, stage);
        END IF;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_license_approvals_stage
    ON license_approvals (stage);

-- ---------------------------------------------------------
-- Seed the initial approvers (idempotent upsert).
-- There is exactly one active Tower Head and one active IT / License
-- Provisioning approver for the initial workflow.
-- ---------------------------------------------------------
INSERT INTO tower_head_approvers (name, email, role, is_active, created_at, updated_at)
VALUES ('Akshayaprasanth', 'akshayap@clouddestinations.com', 'Tower Head', TRUE, NOW(), NOW())
ON CONFLICT (email) DO UPDATE
SET name = EXCLUDED.name,
    role = EXCLUDED.role,
    is_active = TRUE,
    updated_at = NOW();

INSERT INTO license_provisioning_approvers (name, email, role, is_active, created_at, updated_at)
VALUES ('Jerry Joel', 'jerryj@clouddestinations.com', 'IT / License Provisioning', TRUE, NOW(), NOW())
ON CONFLICT (email) DO UPDATE
SET name = EXCLUDED.name,
    role = EXCLUDED.role,
    is_active = TRUE,
    updated_at = NOW();
