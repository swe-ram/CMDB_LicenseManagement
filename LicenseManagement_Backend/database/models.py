
from datetime import datetime, date

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Float,
    Text,
    Index,
    UniqueConstraint,
)

from sqlalchemy.orm import relationship

from database.database import Base


# =========================================================
# Approval workflow constants
# =========================================================
# Two sequential approval stages. These identifiers are stored on each
# ``LicenseApproval.stage`` record and are part of the public API contract.
APPROVAL_STAGE_TOWER_HEAD = "TOWER_HEAD"
APPROVAL_STAGE_IT_LICENSE_PROVISIONING = "IT_LICENSE_PROVISIONING"

# Ordered list of stages for timeline rendering / sequencing.
APPROVAL_STAGE_SEQUENCE = (
    APPROVAL_STAGE_TOWER_HEAD,
    APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
)

# Approval decision/status values. The project already uses title-case
# ("Pending"/"Approved"/"Rejected"), so the workflow reuses those values and
# adds "Not Started" for a stage that has not been activated yet. This keeps
# existing request APIs and the approval table backward compatible.
APPROVAL_STATUS_PENDING = "Pending"
APPROVAL_STATUS_APPROVED = "Approved"
APPROVAL_STATUS_REJECTED = "Rejected"
APPROVAL_STATUS_NOT_STARTED = "Not Started"

# Overall LicenseRequest.status values. "Pending Approval"/"Approved"/"Rejected"
# already exist; "Pending IT Approval" is added for the state after the Tower
# Head approves and the request is awaiting IT / License Provisioning.
REQUEST_STATUS_PENDING_TOWER_HEAD = "Pending Approval"
REQUEST_STATUS_PENDING_PROVISIONING = "Pending IT Approval"
REQUEST_STATUS_APPROVED = "Approved"
REQUEST_STATUS_REJECTED = "Rejected"


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    vendor = Column(String(255))
    category = Column(String(100))
    owner = Column(String(255))
    environment = Column(String(100))
    status = Column(String(50), default="Active")
    last_sync = Column(DateTime)

    licenses = relationship(
        "License",
        back_populates="application",
        cascade="all, delete-orphan",
    )


class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
    )

    product = Column(String(255))
    license_type = Column(String(100))

    purchased_qty = Column(Integer)
    assigned_qty = Column(Integer)
    available_qty = Column(Integer)
    used_qty = Column(Integer)

    cost = Column(Float)
    currency = Column(String(20))
    billing_cycle = Column(String(50))
    renewal_date = Column(Date)

    data_source = Column(String(255))
    last_updated = Column(DateTime, default=datetime.utcnow)

    application = relationship(
        "Application",
        back_populates="licenses",
    )


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    active = Column(Boolean, nullable=False, default=True)

    users = relationship(
        "User",
        back_populates="department",
    )
    approver_mapping = relationship(
        "DepartmentApprover",
        back_populates="department",
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=True,
    )

    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=True,
        index=True,
    )

    external_user_id = Column(String(255))
    name = Column(String(255))
    email = Column(String(255))
    status = Column(String(50))
    role = Column(String(100))
    job_title = Column(String(255))
    last_activity = Column(DateTime)

    department = relationship(
        "Department",
        back_populates="users",
    )
    submitted_license_requests = relationship(
        "LicenseRequest",
        foreign_keys="LicenseRequest.user_id",
        back_populates="employee",
    )
    department_approver_mappings = relationship(
        "DepartmentApprover",
        back_populates="employee",
        foreign_keys="DepartmentApprover.employee_id",
    )


class DepartmentApprover(Base):
    __tablename__ = "department_approvers"

    id = Column(Integer, primary_key=True, index=True)
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
        index=True,
    )
    employee_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    approver_name = Column(String(255), nullable=False)
    approver_email = Column(String(255))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    department = relationship("Department", back_populates="approver_mapping")
    employee = relationship(
        "User",
        back_populates="department_approver_mappings",
        foreign_keys=[employee_id],
    )
    approvals = relationship("LicenseApproval", back_populates="approver")


class LicenseRequest(Base):
    __tablename__ = "license_requests"
    __table_args__ = (
        Index("uq_license_requests_request_number", "request_number", unique=True),
        Index(
            "uq_license_requests_employee_idempotency",
            "employee_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column("request_number", String(64), nullable=False)
    idempotency_key = Column(String(64))
    user_id = Column("employee_id", Integer, ForeignKey("users.id"), nullable=False, index=True)
    department_id = Column(
        Integer,
        ForeignKey("departments.id"),
        nullable=False,
        index=True,
    )
    license_id = Column(Integer, ForeignKey("licenses.id"), nullable=False, index=True)
    approver_id = Column(
        Integer,
        ForeignKey("department_approvers.id"),
        nullable=True,
        index=True,
    )
    business_justification = Column(Text, nullable=False)
    urgency = Column(String(64), nullable=False, default="Standard")
    status = Column(String(30), default="Pending Approval", index=True)
    requested_at = Column("submission_timestamp", DateTime, default=datetime.utcnow)
    provisioning_status = Column(String(50))
    failure_details = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    requested_duration = Column(String(50))
    requester_identity = Column(String(255))

    employee = relationship("User", foreign_keys=[user_id], back_populates="submitted_license_requests")
    department = relationship("Department")
    license = relationship("License")
    approver = relationship("DepartmentApprover", foreign_keys=[approver_id])
    approvals = relationship(
        "LicenseApproval",
        back_populates="request",
        order_by="LicenseApproval.created_at",
    )


class TowerHeadApprover(Base):
    """Configured Tower Head approver for the first approval stage.

    A single active Tower Head approver is used for every department in the
    initial two-stage workflow.
    """

    __tablename__ = "tower_head_approvers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    role = Column(String(100), nullable=False, default="Tower Head")
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    approvals = relationship("LicenseApproval", back_populates="tower_head_approver")


class LicenseProvisioningApprover(Base):
    """Configured IT / License Provisioning approver for the second stage.

    This stage becomes actionable only after the Tower Head approves the
    request.
    """

    __tablename__ = "license_provisioning_approvers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    role = Column(String(100), nullable=False, default="IT / License Provisioning")
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    approvals = relationship("LicenseApproval", back_populates="provisioning_approver")


class LicenseApproval(Base):
    __tablename__ = "license_approvals"
    __table_args__ = (
        # One approval record per request per stage. Enforced additively by the
        # migration; declared here so fresh databases created via
        # ``Base.metadata.create_all`` get the constraint too.
        UniqueConstraint(
            "request_id",
            "stage",
            name="uq_license_approvals_request_stage",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(
        Integer,
        ForeignKey("license_requests.id"),
        nullable=False,
        index=True,
    )
    # ``approver_id`` keeps pointing at ``department_approvers`` for the legacy
    # single-stage flow. It is now nullable so the sequential workflow can store
    # its own approver references (see the two columns below) without a
    # department-approver mapping row.
    approver_id = Column(
        Integer,
        ForeignKey("department_approvers.id"),
        nullable=True,
        index=True,
    )
    # Optional references to the dedicated approver-configuration tables used by
    # the two-stage sequential workflow.
    tower_head_approver_id = Column(
        Integer,
        ForeignKey("tower_head_approvers.id"),
        nullable=True,
        index=True,
    )
    provisioning_approver_id = Column(
        Integer,
        ForeignKey("license_provisioning_approvers.id"),
        nullable=True,
        index=True,
    )
    # Approval stage identifier: "TOWER_HEAD" or "IT_LICENSE_PROVISIONING".
    # Legacy rows created before this column existed store NULL; callers treat a
    # NULL stage as the historical "Department / Tower Head Approval" stage.
    stage = Column(String(40), nullable=True, index=True)
    # Snapshot of the approver's name/email at decision time so history is
    # preserved even if the approver configuration later changes.
    approver_name = Column(String(255))
    approver_email = Column(String(255))
    status = Column("decision", String(20), nullable=False, default="Pending", index=True)
    comments = Column(Text)
    decision_at = Column("decision_timestamp", DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime)

    request = relationship("LicenseRequest", back_populates="approvals")
    approver = relationship("DepartmentApprover", back_populates="approvals")
    tower_head_approver = relationship("TowerHeadApprover", back_populates="approvals")
    provisioning_approver = relationship(
        "LicenseProvisioningApprover", back_populates="approvals"
    )

    @property
    def display_stage(self) -> str:
        """Human-readable stage label used by the approval-flow drawer."""
        if self.stage == APPROVAL_STAGE_IT_LICENSE_PROVISIONING:
            return "IT / License Provisioning"
        return "Tower Head"


class LicenseAssignment(Base):
    __tablename__ = "license_assignments"

    id = Column(Integer, primary_key=True, index=True)

    license_id = Column(
        Integer,
        ForeignKey("licenses.id"),
    )

    user_id = Column(
        Integer,
        ForeignKey("users.id"),
    )

    assigned_date = Column(DateTime)
    status = Column(String(50))


class LicenseAssignedUser(Base):
    __tablename__ = "license_assigned_users"

    __table_args__ = (
        UniqueConstraint(
            "license_id",
            "user_id",
            name="uq_license_assigned_users_license_user",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
        index=True,
    )

    license_id = Column(
        Integer,
        ForeignKey("licenses.id"),
        nullable=False,
        index=True,
    )

    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    sku_id = Column(String(255))
    display_name = Column(String(255))
    email = Column(String(255))

    # Keep this as a text snapshot for existing license assignment data.
    department = Column(String(255))

    external_user_id = Column(String(255))
    status = Column(String(50), default="assigned")
    assigned_at = Column(DateTime, default=datetime.utcnow)
    last_synced_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    application = relationship("Application")
    license = relationship("License")
    user = relationship("User")


class Microsoft365Data(Base):
    __tablename__ = "microsoft365_data"

    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "sku_id",
            name="uq_microsoft365_data_application_sku",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
        index=True,
    )

    sku_id = Column(String(255), nullable=False)
    product = Column(String(255))
    sku_part_number = Column(String(255))
    license_type = Column(String(255))
    purchased_quantity = Column(Integer)
    assigned_quantity = Column(Integer)
    available_quantity = Column(Integer)
    consumed_quantity = Column(Integer)

    synced_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    application = relationship("Application")


class SlackData(Base):
    __tablename__ = "slack_data"

    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "plan",
            "license_type",
            name="uq_slack_data_application_plan_type",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
        index=True,
    )

    plan = Column(String(255))
    license_type = Column(String(255))
    purchased_quantity = Column(Integer)
    entitled_quantity = Column(Integer)
    assigned_quantity = Column(Integer)
    available_quantity = Column(Integer)
    active_users = Column(Integer)
    inactive_users = Column(Integer)
    renewal_date = Column(Date)
    cost = Column(Float)
    currency = Column(String(20))
    billing_cycle = Column(String(50))
    data_source = Column(String(255))

    synced_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    application = relationship("Application")


class Usage(Base):
    __tablename__ = "usage"

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
    )

    user_id = Column(
        Integer,
        ForeignKey("users.id"),
    )

    last_activity = Column(DateTime)
    usage_status = Column(String(50))
    usage_count = Column(Integer)

    last_updated = Column(
        DateTime,
        default=datetime.utcnow,
    )


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
    )

    contract_id = Column(String(255))
    vendor = Column(String(255))
    purchased_qty = Column(Integer)
    cost = Column(Float)
    currency = Column(String(20))
    renewal_date = Column(Date)
    source_document = Column(Text)


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, index=True)

    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
    )

    sync_time = Column(
        DateTime,
        default=datetime.utcnow,
    )

    status = Column(String(50))
    records_processed = Column(Integer)
    error_message = Column(Text)
