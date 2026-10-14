from datetime import datetime

from typing import Literal

from pydantic import BaseModel, Field


class LicenseRequestCreate(BaseModel):
    user_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=16, max_length=64)
    department: str = Field(min_length=1, max_length=255)
    application_name: str = Field(min_length=1, max_length=255)
    license_id: int = Field(gt=0)
    business_justification: str = Field(min_length=1, max_length=10000)
    urgency: Literal["Standard", "Urgent", "Immediate Project Blocker"] = "Standard"


class ApprovalDecision(BaseModel):
    decision: Literal["Approved", "Rejected"]
    comments: str | None = Field(default=None, max_length=5000)


class StageDecision(BaseModel):
    """Decision payload for a specific sequential approval stage."""

    decision: Literal["Approved", "Rejected"]
    comments: str | None = Field(default=None, max_length=5000)


class DepartmentApproverAssignment(BaseModel):
    department_id: int = Field(gt=0)
    approver_user_id: int = Field(gt=0)


class ApproverConfigRead(BaseModel):
    """Public representation of a configured workflow approver."""

    id: int
    name: str
    email: str
    role: str
    is_active: bool


class ApprovalRead(BaseModel):
    id: int
    approver_name: str
    approver_email: str | None = None
    decision: str
    stage: str
    stage_label: str | None = None
    decision_at: datetime | None
    comments: str | None
    created_at: datetime | None
    can_decide: bool = False


class LicenseRequestRead(BaseModel):
    id: int
    request_id: str
    user_id: int
    employee_name: str
    employee_email: str | None
    department: str
    application: str
    license_id: int
    license_type: str | None
    license_name: str | None
    business_justification: str
    urgency: str
    requested_at: datetime | None
    status: str
    provisioning_status: str | None = None
    approver_id: int | None
    approver_name: str | None
    requester_identity: str | None = None
    test_mode: bool = False
    can_decide: bool = False
    # Sequential workflow helper fields (additive; safe for existing clients).
    current_stage: str | None = None
    tower_head_status: str | None = None
    provisioning_status_stage: str | None = None
    email_notifications: list[dict] = Field(default_factory=list)


class LicenseRequestDetail(LicenseRequestRead):
    approvals: list[ApprovalRead] = Field(default_factory=list)
