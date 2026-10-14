from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from auth.session import (
    AuthenticatedPrincipal,
    get_current_principal,
    is_loopback_request,
    local_license_request_test_mode_enabled,
    require_role,
)
from database.approver_repository import (
    get_active_provisioning_approver,
    get_active_tower_head,
)
from database.database import get_db
from database.models import (
    Application,
    Department,
    DepartmentApprover,
    License,
    LicenseApproval,
    LicenseProvisioningApprover,
    LicenseRequest,
    TowerHeadApprover,
    User,
    APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
    APPROVAL_STAGE_TOWER_HEAD,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_NOT_STARTED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_PENDING_PROVISIONING,
    REQUEST_STATUS_PENDING_TOWER_HEAD,
    REQUEST_STATUS_REJECTED,
)
from schemas.license_requests import (
    ApprovalDecision,
    ApprovalRead,
    ApproverConfigRead,
    DepartmentApproverAssignment,
    LicenseRequestCreate,
    LicenseRequestDetail,
    LicenseRequestRead,
    StageDecision,
)
from services import approval_notifications


router = APIRouter(prefix="/api/v1/license-requests", tags=["License Requests"])
legacy_router = APIRouter(tags=["License Requests"])

REQUESTER_ROLE = "License.Requester"
APPROVER_ROLE = "License.Approver"
ADMIN_ROLE = "License.Admin"


def _is_local_test_session(
    principal: AuthenticatedPrincipal,
    http_request: Request,
) -> bool:
    return (
        principal.claims.get("account_type") == "shared"
        and bool(principal.claims.get("license_request_test_mode"))
        and local_license_request_test_mode_enabled()
        and is_loopback_request(http_request)
    )


def _principal_email(principal: AuthenticatedPrincipal) -> str:
    """Return the authenticated principal's email (lower-cased) if present."""
    for key in ("preferred_username", "email", "upn"):
        value = principal.claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _is_approver_session(principal: AuthenticatedPrincipal) -> bool:
    """True when the session belongs to a configured workflow approver.

    Approver sessions use ``account_type == "approver"`` and carry the
    approver's email in ``preferred_username``. They are not tied to a portal
    ``User`` row; authorization is enforced by matching that email against the
    configured approver tables on the backend.
    """
    return principal.claims.get("account_type") == "approver"


def _approver_emails(db: Session) -> tuple[str | None, str | None]:
    """Return ``(tower_head_email, provisioning_email)`` lower-cased or None."""
    tower_head = get_active_tower_head(db)
    provisioning = get_active_provisioning_approver(db)
    th_email = tower_head.email.strip().lower() if tower_head else None
    pr_email = provisioning.email.strip().lower() if provisioning else None
    return th_email, pr_email


def _is_configured_approver(
    principal: AuthenticatedPrincipal, db: Session
) -> bool:
    """True when the principal's email matches a configured approver email."""
    email = _principal_email(principal)
    if not email:
        return False
    th_email, pr_email = _approver_emails(db)
    return email in {value for value in (th_email, pr_email) if value}


def _require_stage_approver(
    principal: AuthenticatedPrincipal,
    http_request: Request,
    db: Session,
    stage: str,
) -> tuple[str, str]:
    """Authorize the caller to decide ``stage`` and return (name, email).

    Authorization is enforced on the backend: the authenticated principal's
    email must match the single active configured approver for the requested
    stage. Local test-mode sessions are explicitly rejected so test mode cannot
    weaken production authorization.
    """
    if _is_local_test_session(principal, http_request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The shared development account cannot make approval decisions.",
        )

    principal_email = _principal_email(principal)
    if not principal_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your session does not identify an approver email.",
        )

    if stage == APPROVAL_STAGE_TOWER_HEAD:
        approver = get_active_tower_head(db)
        role_label = "Tower Head"
    elif stage == APPROVAL_STAGE_IT_LICENSE_PROVISIONING:
        approver = get_active_provisioning_approver(db)
        role_label = "IT / License Provisioning"
    else:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail="Unknown approval stage.")

    if approver is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No active {role_label} approver is configured.",
        )

    if principal_email != approver.email.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only the configured {role_label} approver can make this decision.",
        )

    return approver.name, approver.email


def _identity_user_ids(db: Session, principal: AuthenticatedPrincipal) -> set[int]:
    if principal.claims.get("account_type") == "shared":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The shared account cannot act as an individual employee or approver.",
        )

    users = (
        db.query(User)
        .filter(User.external_user_id == principal.object_id)
        .order_by(User.id)
        .all()
    )
    active_users = [
        user
        for user in users
        if (user.status or "active").strip().lower() not in {"inactive", "disabled"}
    ]
    if not active_users:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This shared account is not mapped to an individual portal employee.",
        )
    return {user.id for user in active_users}


def _accessible_request(
    db: Session,
    request_id: str,
    principal: AuthenticatedPrincipal,
    http_request: Request,
) -> tuple[LicenseRequest, set[int]]:
    local_test = _is_local_test_session(principal, http_request)
    # Approver sessions are authorized by email against the configured approver
    # tables (enforced again on each decision endpoint). They may view requests
    # to make decisions, so they do not need a portal ``User`` row.
    approver_session = _is_approver_session(principal) and _is_configured_approver(
        principal, db
    )
    user_ids = (
        set()
        if (local_test or approver_session)
        else _identity_user_ids(db, principal)
    )
    request = (
        db.query(LicenseRequest)
        .options(
            joinedload(LicenseRequest.employee),
            joinedload(LicenseRequest.department),
            joinedload(LicenseRequest.license).joinedload(License.application),
            joinedload(LicenseRequest.approver).joinedload(DepartmentApprover.employee),
            joinedload(LicenseRequest.approvals).joinedload(LicenseApproval.approver),
        )
        .filter(LicenseRequest.request_id == request_id)
        .first()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="License request not found.")
    if local_test or approver_session:
        return request, set()
    if (
        ADMIN_ROLE not in principal.roles
        and request.user_id not in user_ids
        and (
            request.approver is None
            or request.approver.employee_id not in user_ids
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to view this license request.",
        )
    return request, user_ids


def _approval_can_decide(
    db: Session,
    principal: AuthenticatedPrincipal,
    http_request: Request,
    approval: LicenseApproval,
    request: LicenseRequest,
) -> bool:
    """Whether ``principal`` is currently allowed to decide this approval stage.

    True only when the caller is the configured approver for the stage, the
    stage is Pending, and (for the provisioning stage) the Tower Head stage has
    already been approved. Local test-mode sessions can never decide.
    """
    if _is_local_test_session(principal, http_request):
        return False
    if approval.status != APPROVAL_STATUS_PENDING:
        return False
    if approval.stage == APPROVAL_STAGE_TOWER_HEAD:
        approver = get_active_tower_head(db)
    elif approval.stage == APPROVAL_STAGE_IT_LICENSE_PROVISIONING:
        # Provisioning is only actionable after the Tower Head approved.
        if (
            _stage_status(request, APPROVAL_STAGE_TOWER_HEAD)
            != APPROVAL_STATUS_APPROVED
        ):
            return False
        approver = get_active_provisioning_approver(db)
    else:
        return False
    if approver is None:
        return False
    return _principal_email(principal) == approver.email.strip().lower()


def _can_decide(
    request: LicenseRequest,
    principal: AuthenticatedPrincipal,
    user_ids: set[int],
) -> bool:
    return (
        APPROVER_ROLE in principal.roles
        and request.status == "Pending Approval"
        and request.approver is not None
        and request.approver.employee_id in user_ids
    )


def _stage_status(request: LicenseRequest, stage: str) -> str | None:
    """Return the status of the approval record for ``stage`` or None."""
    for approval in request.approvals:
        if approval.stage == stage:
            return approval.status
    return None


def _current_stage(request: LicenseRequest) -> str | None:
    """Return the stage that is currently actionable, if any."""
    if request.status in {REQUEST_STATUS_REJECTED, REQUEST_STATUS_APPROVED}:
        return None
    tower_head_status = _stage_status(request, APPROVAL_STAGE_TOWER_HEAD)
    provisioning_status = _stage_status(
        request, APPROVAL_STAGE_IT_LICENSE_PROVISIONING
    )
    if tower_head_status == APPROVAL_STATUS_PENDING:
        return APPROVAL_STAGE_TOWER_HEAD
    if (
        tower_head_status == APPROVAL_STATUS_APPROVED
        and provisioning_status == APPROVAL_STATUS_PENDING
    ):
        return APPROVAL_STAGE_IT_LICENSE_PROVISIONING
    return None


def _serialize_request(
    request: LicenseRequest,
    principal: AuthenticatedPrincipal,
    user_ids: set[int],
    local_test: bool,
) -> dict:
    employee = request.employee
    return {
        "id": request.id,
        "request_id": request.request_id,
        "user_id": request.user_id,
        "employee_name": (
            employee.name or employee.email or "Employee"
            if employee
            else "Unknown employee"
        ),
        "employee_email": employee.email if employee else None,
        "department": request.department.name,
        "application": request.license.application.name,
        "license_id": request.license_id,
        "license_type": request.license.license_type,
        "license_name": request.license.product,
        "business_justification": request.business_justification,
        "urgency": request.urgency,
        "requested_at": request.requested_at or request.created_at,
        "status": request.status or "Unknown",
        "provisioning_status": request.provisioning_status,
        "approver_id": request.approver_id,
        "approver_name": (
            (request.approver.approver_name or request.approver.approver_email)
            if request.approver
            else None
        ),
        "requester_identity": request.requester_identity,
        "test_mode": local_test,
        "can_decide": _can_decide(request, principal, user_ids),
        "current_stage": _current_stage(request),
        "tower_head_status": _stage_status(
            request, APPROVAL_STAGE_TOWER_HEAD
        ),
        "provisioning_status_stage": _stage_status(
            request, APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        ),
    }


def _serialize_approval(
    approval: LicenseApproval,
    can_decide: bool = False,
) -> dict:
    approver_name = (
        approval.approver_name
        or (
            approval.approver.approver_name
            or approval.approver.approver_email
            if approval.approver
            else None
        )
        or "Approver"
    )
    approver_email = approval.approver_email or (
        approval.approver.approver_email if approval.approver else None
    )
    # Legacy rows have a NULL stage; surface the historical stage identifier so
    # existing clients keep working.
    stage = approval.stage or "Department / Tower Head Approval"
    return {
        "id": approval.id,
        "approver_name": approver_name,
        "approver_email": approver_email,
        "decision": approval.status,
        "stage": stage,
        "stage_label": approval.display_stage
        if approval.stage
        else "Department / Tower Head Approval",
        "decision_at": approval.decision_at,
        "comments": approval.comments,
        "created_at": approval.created_at,
        "can_decide": can_decide,
    }


def _create_request(
    payload: LicenseRequestCreate,
    db: Session,
    principal: AuthenticatedPrincipal,
    http_request: Request,
) -> LicenseRequestDetail:
    local_test = _is_local_test_session(principal, http_request)
    if (
        not local_test
        and REQUESTER_ROLE not in principal.roles
        and ADMIN_ROLE not in principal.roles
    ):
        require_role(principal, REQUESTER_ROLE)
    user_ids = set() if local_test else _identity_user_ids(db, principal)
    if not local_test and payload.user_id not in user_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A request can only be submitted for the authenticated employee.",
        )

    existing = (
        db.query(LicenseRequest)
        .filter(
            LicenseRequest.user_id == payload.user_id,
            LicenseRequest.idempotency_key == payload.idempotency_key,
        )
        .first()
    )
    if existing is not None:
        existing, user_ids = _accessible_request(
            db, existing.request_id, principal, http_request
        )
        return LicenseRequestDetail(
            **_serialize_request(existing, principal, user_ids, local_test),
            approvals=[_serialize_approval(item) for item in existing.approvals],
        )

    employee = db.query(User).filter(User.id == payload.user_id).first()
    if employee is None or employee.department_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The employee does not have a department mapped in the database.",
        )

    department = (
        db.query(Department)
        .filter(Department.name.ilike(payload.department.strip()), Department.active.is_(True))
        .first()
    )
    if department is None or department.id != employee.department_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The selected department does not match the employee's database department.",
        )

    application = (
        db.query(Application)
        .filter(Application.name.ilike(payload.application_name.strip()))
        .first()
    )
    if application is None:
        raise HTTPException(status_code=422, detail="The selected application does not exist.")

    license_record = (
        db.query(License)
        .filter(
            License.id == payload.license_id,
            License.application_id == application.id,
        )
        .first()
    )
    if license_record is None:
        raise HTTPException(
            status_code=422,
            detail="The selected license does not belong to the selected application.",
        )

    # The sequential workflow uses globally configured approvers (a single Tower
    # Head and a single IT / License Provisioning approver for every department).
    # A department-approver mapping is no longer required, but we still populate
    # ``approver_id`` when one exists so legacy displays keep working.
    mapping = None
    tower_head_approver = None
    provisioning_approver = None
    if not local_test:
        mapping = (
            db.query(DepartmentApprover)
            .options(joinedload(DepartmentApprover.employee))
            .filter(
                DepartmentApprover.department_id == department.id,
                DepartmentApprover.active.is_(True),
            )
            .first()
        )
        tower_head_approver = get_active_tower_head(db)
        provisioning_approver = get_active_provisioning_approver(db)
        if tower_head_approver is None or provisioning_approver is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "The approval workflow is not fully configured. Ensure an active "
                    "Tower Head approver and an active IT / License Provisioning "
                    "approver exist."
                ),
            )
    if mapping is not None and (
        not mapping.employee.external_user_id
        or (mapping.employee.status or "active").strip().lower() in {"inactive", "disabled"}
    ):
        # A stale/invalid mapping must not block the workflow; ignore it for the
        # sequential flow (it is only used for the optional legacy approver link).
        mapping = None

    now = datetime.utcnow()
    request = LicenseRequest(
        request_id=f"PENDING-{uuid4().hex}",
        idempotency_key=payload.idempotency_key,
        user_id=employee.id,
        department_id=department.id,
        license_id=license_record.id,
        approver_id=mapping.id if mapping else None,
        business_justification=payload.business_justification.strip(),
        urgency=payload.urgency.strip(),
        status=(
            REQUEST_STATUS_PENDING_TOWER_HEAD
            if not local_test
            else "Pending Approval"
        ),
        requested_at=now,
        provisioning_status="Not Started" if local_test else None,
        requester_identity=(
            "shared-development-account" if local_test else None
        ),
        created_at=now,
        updated_at=now,
    )
    db.add(request)
    try:
        db.flush()
        request.request_id = f"LIC-{request.created_at.year}-{request.id:06d}"

        # Create the two sequential approval records. Outside local test mode:
        #   * Tower Head stage starts PENDING and is actionable immediately.
        #   * IT / License Provisioning stage starts NOT_STARTED and only becomes
        #     PENDING after the Tower Head approves.
        # Test-mode requests intentionally create no approval records (preserving
        # the existing non-persistent preview behavior).
        if not local_test:
            tower_head_approval = LicenseApproval(
                request=request,
                stage=APPROVAL_STAGE_TOWER_HEAD,
                approver_id=None,
                tower_head_approver_id=tower_head_approver.id,
                provisioning_approver_id=None,
                approver_name=tower_head_approver.name,
                approver_email=tower_head_approver.email,
                status=APPROVAL_STATUS_PENDING,
                created_at=now,
                updated_at=now,
            )
            provisioning_approval = LicenseApproval(
                request=request,
                stage=APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
                approver_id=None,
                tower_head_approver_id=None,
                provisioning_approver_id=provisioning_approver.id,
                approver_name=provisioning_approver.name,
                approver_email=provisioning_approver.email,
                status=APPROVAL_STATUS_NOT_STARTED,
                created_at=now,
                updated_at=now,
            )
            db.add_all([tower_head_approval, provisioning_approval])

        db.commit()
        db.refresh(request)
    except IntegrityError as exc:
        db.rollback()
        existing = (
            db.query(LicenseRequest)
            .filter(
                LicenseRequest.user_id == payload.user_id,
                LicenseRequest.idempotency_key == payload.idempotency_key,
            )
            .first()
        )
        if existing is not None:
            existing, user_ids = _accessible_request(
                db, existing.request_id, principal, http_request
            )
            return LicenseRequestDetail(
                **_serialize_request(existing, principal, user_ids, local_test),
                approvals=[_serialize_approval(item) for item in existing.approvals],
            )
        raise HTTPException(
            status_code=409,
            detail="The request could not be saved because of a database uniqueness conflict.",
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="The license request could not be saved.",
        ) from exc

    # The request and both approval records are now durably committed. Notify the
    # Tower Head by email. A delivery failure must NOT lose the saved request, so
    # the email result is captured and surfaced rather than raised.
    email_notifications: list[dict] = []
    if not local_test:
        result = approval_notifications.notify_tower_head_approval_request(
            request_id=request.request_id,
            employee_name=employee.name or employee.email or "Employee",
            employee_email=employee.email,
            department=department.name,
            application_name=application.name,
            license_name=license_record.product or license_record.license_type,
            business_justification=request.business_justification,
            urgency=request.urgency,
            approver_email=tower_head_approver.email,
            approver_name=tower_head_approver.name,
        )
        email_notifications.append(
            {"stage": APPROVAL_STAGE_TOWER_HEAD, **result.as_dict()}
        )

    db.refresh(request)
    return LicenseRequestDetail(
        **_serialize_request(
            request, principal, user_ids, local_test
        ),
        approvals=[_serialize_approval(item) for item in request.approvals],
        email_notifications=email_notifications,
    )


@router.post("", response_model=LicenseRequestDetail, status_code=status.HTTP_201_CREATED)
def create_license_request(
    payload: LicenseRequestCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    return _create_request(payload, db, principal, http_request)


# =========================================================
# Sequential workflow: approver configuration
# =========================================================
# These static routes are declared before the ``/{request_id}`` routes so that
# FastAPI's ordered router does not treat "approvers" as a request id.


@router.get("/approvers/tower-head", response_model=ApproverConfigRead)
def get_tower_head_approver(
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> ApproverConfigRead:
    approver = get_active_tower_head(db)
    if approver is None:
        raise HTTPException(
            status_code=404,
            detail="No active Tower Head approver is configured.",
        )
    return ApproverConfigRead(
        id=approver.id,
        name=approver.name,
        email=approver.email,
        role=approver.role,
        is_active=approver.is_active,
    )


@router.get("/approvers/it-license-provisioning", response_model=ApproverConfigRead)
def get_provisioning_approver(
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> ApproverConfigRead:
    approver = get_active_provisioning_approver(db)
    if approver is None:
        raise HTTPException(
            status_code=404,
            detail="No active IT / License Provisioning approver is configured.",
        )
    return ApproverConfigRead(
        id=approver.id,
        name=approver.name,
        email=approver.email,
        role=approver.role,
        is_active=approver.is_active,
    )



@router.get("/options")
def get_license_request_options(
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> dict:
    local_test = _is_local_test_session(principal, http_request)
    if (
        not local_test
        and REQUESTER_ROLE not in principal.roles
        and ADMIN_ROLE not in principal.roles
    ):
        require_role(principal, REQUESTER_ROLE)
    if local_test:
        employees = (
            db.query(User)
            .join(Department, User.department_id == Department.id)
            .filter(
                User.department_id.isnot(None),
                Department.active.is_(True),
                (User.status.is_(None))
                | (
                    ~User.status.ilike("inactive")
                    & ~User.status.ilike("disabled")
                ),
            )
            .order_by(User.name, User.id)
            .all()
        )
    else:
        user_ids = _identity_user_ids(db, principal)
        employees = (
            db.query(User)
            .filter(User.id.in_(user_ids), User.department_id.isnot(None))
            .order_by(User.id)
            .all()
        )
    if not employees:
        raise HTTPException(
            status_code=409,
            detail="No active employees with department mappings are available.",
        )

    licenses = (
        db.query(License, Application.name)
        .join(Application, License.application_id == Application.id)
        .order_by(Application.name, License.product)
        .all()
    )
    return {
        "users": [
            {
                "user_id": employee.id,
                "name": employee.name or employee.email or "Employee",
                "email": employee.email,
                "department": employee.department.name,
                "role": employee.role or "Employee",
            }
            for employee in employees
        ],
        "licenses": [
            {
                "license_id": license.id,
                "db_license_id": license.id,
                "license_name": license.product,
                "product": license.product,
                "license_type": license.license_type,
                "application_name": application_name,
                "available_quantity": license.available_qty,
            }
            for license, application_name in licenses
        ],
        "test_mode": local_test,
    }


@legacy_router.post(
    "/licenses/request",
    response_model=LicenseRequestDetail,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_license_request_legacy(
    payload: LicenseRequestCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    return _create_request(payload, db, principal, http_request)


@router.get("", response_model=list[LicenseRequestRead])
def list_license_requests(
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> list[LicenseRequestRead]:
    local_test = _is_local_test_session(principal, http_request)
    user_ids = set() if local_test else _identity_user_ids(db, principal)
    query = db.query(LicenseRequest)
    if local_test:
        pass
    elif ADMIN_ROLE not in principal.roles:
        if APPROVER_ROLE in principal.roles:
            query = query.filter(
                (LicenseRequest.user_id.in_(user_ids))
                | LicenseRequest.approver.has(
                    DepartmentApprover.employee_id.in_(user_ids)
                )
            )
        elif REQUESTER_ROLE in principal.roles:
            query = query.filter(LicenseRequest.user_id.in_(user_ids))
        else:
            raise HTTPException(status_code=403, detail="No license request viewing role is assigned.")

    requests = (
        query.options(
            joinedload(LicenseRequest.employee),
            joinedload(LicenseRequest.department),
            joinedload(LicenseRequest.license).joinedload(License.application),
            joinedload(LicenseRequest.approver).joinedload(DepartmentApprover.employee),
        )
        .order_by(LicenseRequest.created_at.desc(), LicenseRequest.id.desc())
        .all()
    )
    return [
        LicenseRequestRead(
            **_serialize_request(request, principal, user_ids, local_test)
        )
        for request in requests
    ]


@router.get("/{request_id}/approvals", response_model=list[ApprovalRead])
def get_approval_history(
    request_id: str,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> list[ApprovalRead]:
    request, _ = _accessible_request(db, request_id, principal, http_request)
    return [
        ApprovalRead(
            **_serialize_approval(
                item,
                can_decide=_approval_can_decide(
                    db, principal, http_request, item, request
                ),
            )
        )
        for item in request.approvals
    ]


@router.get("/{request_id}", response_model=LicenseRequestDetail)
def get_license_request(
    request_id: str,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    request, user_ids = _accessible_request(
        db, request_id, principal, http_request
    )
    return LicenseRequestDetail(
        **_serialize_request(
            request,
            principal,
            user_ids,
            _is_local_test_session(principal, http_request),
        ),
        approvals=[
            _serialize_approval(
                item,
                can_decide=_approval_can_decide(
                    db, principal, http_request, item, request
                ),
            )
            for item in request.approvals
        ],
    )


def _decide_stage(
    request_id: str,
    stage: str,
    payload: StageDecision,
    http_request: Request,
    db: Session,
    principal: AuthenticatedPrincipal,
) -> LicenseRequestDetail:
    """Approve or reject a specific sequential approval stage.

    Enforces, in order:
      1. The caller is the configured approver for ``stage`` (by email).
      2. The request exists.
      3. The requested stage is currently Pending (no duplicate decisions).
      4. For the provisioning stage, the Tower Head stage is already Approved.
    Uses row locking to prevent concurrent duplicate decisions.
    """
    # (1) Authorization - backend enforced by email match.
    approver_name, approver_email = _require_stage_approver(
        principal, http_request, db, stage
    )

    # (2) Lock the request row to serialize concurrent decisions.
    request = (
        db.query(LicenseRequest)
        .filter(LicenseRequest.request_id == request_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="License request not found.")

    # (3) Lock and load the target stage approval.
    approval = (
        db.query(LicenseApproval)
        .filter(
            LicenseApproval.request_id == request.id,
            LicenseApproval.stage == stage,
        )
        .with_for_update()
        .first()
    )
    if approval is None:
        raise HTTPException(
            status_code=409,
            detail="This approval stage does not exist for the request.",
        )
    if approval.status != APPROVAL_STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail="This approval stage has already been decided.",
        )

    # (4) Stage sequencing rules.
    if stage == APPROVAL_STAGE_IT_LICENSE_PROVISIONING:
        tower_head_status = _stage_status(request, APPROVAL_STAGE_TOWER_HEAD)
        if tower_head_status != APPROVAL_STATUS_APPROVED:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The IT / License Provisioning stage can only be decided after "
                    "the Tower Head approves the request."
                ),
            )

    now = datetime.utcnow()
    approval.status = payload.decision
    approval.comments = payload.comments.strip() if payload.comments else None
    approval.decision_at = now
    approval.approver_name = approver_name
    approval.approver_email = approver_email
    approval.updated_at = now

    email_notifications: list[dict] = []
    requester_email = request.employee.email if request.employee else None

    if stage == APPROVAL_STAGE_TOWER_HEAD:
        if payload.decision == APPROVAL_STATUS_APPROVED:
            # Activate the provisioning stage and advance the request.
            provisioning_approval = (
                db.query(LicenseApproval)
                .filter(
                    LicenseApproval.request_id == request.id,
                    LicenseApproval.stage
                    == APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
                )
                .with_for_update()
                .first()
            )
            if (
                provisioning_approval is not None
                and provisioning_approval.status == APPROVAL_STATUS_NOT_STARTED
            ):
                provisioning_approval.status = APPROVAL_STATUS_PENDING
                provisioning_approval.updated_at = now
            request.status = REQUEST_STATUS_PENDING_PROVISIONING
        else:
            # Rejection ends the workflow; provisioning stays NOT_STARTED.
            request.status = REQUEST_STATUS_REJECTED
    else:  # IT / License Provisioning stage
        request.status = (
            REQUEST_STATUS_APPROVED
            if payload.decision == APPROVAL_STATUS_APPROVED
            else REQUEST_STATUS_REJECTED
        )

    request.updated_at = now
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="The approval decision could not be saved.",
        ) from exc

    # Send notifications AFTER the decision is durably committed. Email failures
    # never roll back the decision; they are captured for a visible notification.
    if (
        stage == APPROVAL_STAGE_TOWER_HEAD
        and payload.decision == APPROVAL_STATUS_APPROVED
    ):
        provisioning_approver = get_active_provisioning_approver(db)
        if provisioning_approver is not None:
            result = approval_notifications.notify_provisioning_approval_request(
                request_id=request.request_id,
                employee_name=(
                    request.employee.name or "Employee"
                    if request.employee
                    else "Employee"
                ),
                employee_email=requester_email,
                department=request.department.name,
                application_name=request.license.application.name,
                license_name=request.license.product
                or request.license.license_type,
                business_justification=request.business_justification,
                urgency=request.urgency,
                tower_head_name=approver_name,
                approver_email=provisioning_approver.email,
                approver_name=provisioning_approver.name,
            )
            email_notifications.append(
                {
                    "stage": APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
                    **result.as_dict(),
                }
            )

    if stage == APPROVAL_STAGE_IT_LICENSE_PROVISIONING or (
        stage == APPROVAL_STAGE_TOWER_HEAD
        and payload.decision == APPROVAL_STATUS_REJECTED
    ):
        # Notify the requester of the final outcome / rejection.
        result = approval_notifications.notify_requester_decision(
            request_id=request.request_id,
            requester_email=requester_email,
            decision=payload.decision,
            stage_label=approval.display_stage,
            approver_name=approver_name,
            comment=approval.comments,
        )
        email_notifications.append(
            {"stage": stage, "audience": "requester", **result.as_dict()}
        )

    request, user_ids = _accessible_request(
        db, request_id, principal, http_request
    )
    return LicenseRequestDetail(
        **_serialize_request(
            request,
            principal,
            user_ids,
            _is_local_test_session(principal, http_request),
        ),
        approvals=[
            _serialize_approval(
                item,
                can_decide=_approval_can_decide(
                    db, principal, http_request, item, request
                ),
            )
            for item in request.approvals
        ],
        email_notifications=email_notifications,
    )


@router.post(
    "/{request_id}/tower-head/decision",
    response_model=LicenseRequestDetail,
)
def decide_tower_head_stage(
    request_id: str,
    payload: StageDecision,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    return _decide_stage(
        request_id,
        APPROVAL_STAGE_TOWER_HEAD,
        payload,
        http_request,
        db,
        principal,
    )


@router.post(
    "/{request_id}/it-license-provisioning/decision",
    response_model=LicenseRequestDetail,
)
def decide_provisioning_stage(
    request_id: str,
    payload: StageDecision,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    return _decide_stage(
        request_id,
        APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
        payload,
        http_request,
        db,
        principal,
    )



@router.post("/{request_id}/decision", response_model=LicenseRequestDetail)
def decide_license_request(
    request_id: str,
    payload: ApprovalDecision,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    require_role(principal, APPROVER_ROLE)
    request, user_ids = _accessible_request(
        db, request_id, principal, http_request
    )
    request = (
        db.query(LicenseRequest)
        .filter(LicenseRequest.id == request.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="License request not found.")
    if request.approver is None or request.approver.employee_id not in user_ids:
        raise HTTPException(status_code=403, detail="This request is not assigned to you.")
    if request.status != "Pending Approval":
        raise HTTPException(status_code=409, detail="This request has already been decided.")

    approval = (
        db.query(LicenseApproval)
        .filter(
            LicenseApproval.request_id == request.id,
            LicenseApproval.approver_id == request.approver_id,
            LicenseApproval.status == "Pending",
        )
        .with_for_update()
        .first()
    )
    if approval is None:
        raise HTTPException(status_code=409, detail="No pending approval stage exists.")

    now = datetime.utcnow()
    approval.status = payload.decision
    approval.comments = payload.comments.strip() if payload.comments else None
    approval.decision_at = now
    request.status = payload.decision
    request.updated_at = now
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="The approval decision could not be saved.") from exc

    request, user_ids = _accessible_request(
        db, request_id, principal, http_request
    )
    return LicenseRequestDetail(
        **_serialize_request(
            request,
            principal,
            user_ids,
            _is_local_test_session(principal, http_request),
        ),
        approvals=[_serialize_approval(item) for item in request.approvals],
    )


@router.put("/department-approvers", status_code=status.HTTP_200_OK)
def assign_department_approver(
    payload: DepartmentApproverAssignment,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> dict[str, str | int]:
    if _is_local_test_session(principal, http_request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The shared test account cannot assign real department approvers.",
        )
    require_role(principal, ADMIN_ROLE)
    _identity_user_ids(db, principal)
    department = db.query(Department).filter(Department.id == payload.department_id).first()
    approver = db.query(User).filter(User.id == payload.approver_user_id).first()
    if department is None or not department.active:
        raise HTTPException(status_code=404, detail="Active department not found.")
    if approver is None or not approver.external_user_id:
        raise HTTPException(
            status_code=422,
            detail="Approver must map to an individually authenticated portal user.",
        )
    mapping = (
        db.query(DepartmentApprover)
        .filter(
            DepartmentApprover.department_id == department.id,
            DepartmentApprover.employee_id == approver.id,
        )
        .first()
    )
    if mapping is None:
        mapping = DepartmentApprover(
            department_id=department.id,
            employee_id=approver.id,
            approver_name=approver.name or approver.email or "Approver",
            approver_email=approver.email,
            active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(mapping)
    for other_mapping in (
        db.query(DepartmentApprover)
        .filter(
            DepartmentApprover.department_id == department.id,
            DepartmentApprover.active.is_(True),
        )
        .all()
    ):
        other_mapping.active = other_mapping.employee_id == approver.id
        other_mapping.updated_at = datetime.utcnow()
    mapping.approver_name = approver.name or approver.email or "Approver"
    mapping.approver_email = approver.email
    mapping.active = True
    mapping.updated_at = datetime.utcnow()
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="The approver mapping could not be saved.") from exc
    return {
        "department_id": department.id,
        "approver_user_id": approver.id,
        "status": "active",
    }
