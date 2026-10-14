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
from database.database import get_db
from database.models import (
    Application,
    Department,
    DepartmentApprover,
    License,
    LicenseApproval,
    LicenseRequest,
    User,
)
from schemas.license_requests import (
    ApprovalDecision,
    ApprovalRead,
    DepartmentApproverAssignment,
    LicenseRequestCreate,
    LicenseRequestDetail,
    LicenseRequestRead,
)


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
    user_ids = set() if local_test else _identity_user_ids(db, principal)
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
    if local_test:
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


def _can_decide(request: LicenseRequest, principal: AuthenticatedPrincipal, user_ids: set[int]) -> bool:
    return (
        APPROVER_ROLE in principal.roles
        and request.status == "Pending Approval"
        and request.approver is not None
        and request.approver.employee_id in user_ids
    )


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
    }


def _serialize_approval(approval: LicenseApproval) -> dict:
    return {
        "id": approval.id,
        "approver_name": (
            approval.approver.approver_name
            or approval.approver.approver_email
            or "Approver"
        ),
        "decision": approval.status,
        "stage": approval.stage,
        "decision_at": approval.decision_at,
        "comments": approval.comments,
        "created_at": approval.created_at,
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

    mapping = None
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
    if mapping is None and not local_test:
        raise HTTPException(
            status_code=409,
            detail="No active approver is mapped to the employee's department.",
        )
    if mapping is not None and (
        not mapping.employee.external_user_id
        or (mapping.employee.status or "active").strip().lower() in {"inactive", "disabled"}
    ):
        raise HTTPException(
            status_code=409,
            detail="The department approver is not mapped to an active portal user.",
        )

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
        status="Pending Approval",
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
        if mapping is not None:
            approval = LicenseApproval(
                request=request,
                approver_id=mapping.id,
                status="Pending",
                created_at=now,
                updated_at=now,
            )
            db.add(approval)
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

    db.refresh(request)
    return LicenseRequestDetail(
        **_serialize_request(request, principal, user_ids, local_test),
        approvals=[_serialize_approval(item) for item in request.approvals],
    )


@router.post("", response_model=LicenseRequestDetail, status_code=status.HTTP_201_CREATED)
def create_license_request(
    payload: LicenseRequestCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> LicenseRequestDetail:
    return _create_request(payload, db, principal, http_request)


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
    return [ApprovalRead(**_serialize_approval(item)) for item in request.approvals]


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
        approvals=[_serialize_approval(item) for item in request.approvals],
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
