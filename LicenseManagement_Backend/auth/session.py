import hmac
import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db


SESSION_COOKIE_NAME = "licensehub_session"
SESSION_ISSUER = "licensehub"
SESSION_AUDIENCE = "licensehub-frontend"
SHARED_ACCOUNT_SUBJECT = "shared-development-account"
APPROVER_ACCOUNT_TYPE = "approver"
SHARED_ACCOUNT_TYPE = "shared"
# Subject prefix for approver sessions. The full subject is
# ``f"{APPROVER_ACCOUNT_SUBJECT_PREFIX}{email}"`` so each approver has a stable,
# distinct subject while still being identifiable as an approver account.
APPROVER_ACCOUNT_SUBJECT_PREFIX = "approver:"
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def local_license_request_test_mode_enabled() -> bool:
    environment = os.getenv("APP_ENV", "").strip().lower()
    enabled = os.getenv("LICENSE_REQUEST_TEST_MODE", "").strip().lower()
    return environment in {"development", "local"} and enabled in {"true", "1", "yes"}


def is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    object_id: str
    roles: frozenset[str]
    claims: dict[str, Any]


class LoginCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


def _auth_settings() -> tuple[str, str, str, int, bool]:
    username = os.getenv("AUTH_USERNAME", "").strip()
    password = os.getenv("AUTH_PASSWORD", "")
    signing_secret = os.getenv("AUTH_TOKEN_SECRET", "")
    try:
        lifetime_minutes = int(os.getenv("AUTH_TOKEN_TTL_MINUTES", "480"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication configuration is invalid.",
        ) from exc

    if (
        not username
        or not password
        or len(signing_secret.encode("utf-8")) < 32
        or not 1 <= lifetime_minutes <= 1440
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shared development authentication is not configured correctly.",
        )

    secure_cookie = os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower() in {
        "true",
        "1",
        "yes",
    }
    return username, password, signing_secret, lifetime_minutes, secure_cookie


def get_current_principal(
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> AuthenticatedPrincipal:
    _, _, signing_secret, _, _ = _auth_settings()
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid LicenseHub session is required.",
        )

    try:
        claims = jwt.decode(
            session_token,
            signing_secret,
            algorithms=["HS256"],
            audience=SESSION_AUDIENCE,
            issuer=SESSION_ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "iss",
                    "aud",
                    "sub",
                    "roles",
                    "account_type",
                    "preferred_username",
                ]
            },
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The LicenseHub session is invalid or expired.",
        ) from exc

    account_type = claims.get("account_type")
    subject = claims.get("sub")

    if account_type == SHARED_ACCOUNT_TYPE:
        if subject != SHARED_ACCOUNT_SUBJECT:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The LicenseHub session is invalid.",
            )
        object_id = SHARED_ACCOUNT_SUBJECT
    elif account_type == APPROVER_ACCOUNT_TYPE:
        # Approver sessions carry a distinct subject of the form
        # "approver:<email>". The email is also present in preferred_username.
        if (
            not isinstance(subject, str)
            or not subject.startswith(APPROVER_ACCOUNT_SUBJECT_PREFIX)
            or not claims.get("preferred_username")
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The LicenseHub session is invalid.",
            )
        object_id = subject
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The LicenseHub session is invalid.",
        )

    token_roles = claims.get("roles", [])
    if not isinstance(token_roles, list) or any(
        not isinstance(role, str) for role in token_roles
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The LicenseHub session is invalid.",
        )
    return AuthenticatedPrincipal(
        object_id=object_id,
        roles=frozenset(token_roles),
        claims=claims,
    )


def require_role(principal: AuthenticatedPrincipal, role: str) -> None:
    if role not in principal.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"The {role} role is required.",
        )


@router.post("/login")
def login(
    credentials: LoginCredentials,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    username, password, signing_secret, lifetime_minutes, secure_cookie = _auth_settings()

    # Determine whether this is a shared-account login or a configured approver
    # login. Approvers authenticate with their own email as the username and the
    # shared portal password. Only configured approver emails are accepted here;
    # authorization for specific stages is enforced again on the backend.
    entered_username = credentials.username.strip()
    is_shared_login = hmac.compare_digest(
        entered_username.encode("utf-8"), username.encode("utf-8")
    )
    valid_password = hmac.compare_digest(
        credentials.password.encode("utf-8"),
        password.encode("utf-8"),
    )

    approver_email: str | None = None
    if not is_shared_login and valid_password:
        approver_email = _configured_approver_email(db, entered_username)

    if not (valid_password and (is_shared_login or approver_email)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The username or password is incorrect.",
        )

    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=lifetime_minutes)
    request_test_mode = (
        local_license_request_test_mode_enabled()
        and is_loopback_request(request)
    )

    if approver_email and not is_shared_login:
        subject = f"{APPROVER_ACCOUNT_SUBJECT_PREFIX}{approver_email}"
        token = jwt.encode(
            {
                "sub": subject,
                "account_type": APPROVER_ACCOUNT_TYPE,
                "preferred_username": approver_email,
                "roles": [],
                "iat": now,
                "exp": expires,
                "iss": SESSION_ISSUER,
                "aud": SESSION_AUDIENCE,
            },
            signing_secret,
            algorithm="HS256",
        )
        user_payload = {
            "username": approver_email,
            "name": approver_email,
            "roles": [],
            "is_shared_account": False,
            "account_type": APPROVER_ACCOUNT_TYPE,
        }
    else:
        token = jwt.encode(
            {
                "sub": SHARED_ACCOUNT_SUBJECT,
                "account_type": SHARED_ACCOUNT_TYPE,
                "preferred_username": username,
                "roles": [],
                "license_request_test_mode": request_test_mode,
                "iat": now,
                "exp": expires,
                "iss": SESSION_ISSUER,
                "aud": SESSION_AUDIENCE,
            },
            signing_secret,
            algorithm="HS256",
        )
        user_payload = {
            "username": username,
            "name": "Shared development account",
            "roles": [],
            "is_shared_account": True,
            "account_type": SHARED_ACCOUNT_TYPE,
            "license_request_test_mode": request_test_mode,
        }

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=lifetime_minutes * 60,
        expires=expires,
        path="/api/v1",
        secure=secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return {"user": user_payload}


def _configured_approver_email(db: Session, entered_username: str) -> str | None:
    """Return the matching active approver email, or None.

    Used during login to decide whether to issue an approver session. Matches
    case-insensitively against the configured Tower Head and IT / License
    Provisioning approver emails.
    """
    from database.approver_repository import (
        get_active_provisioning_approver,
        get_active_tower_head,
    )

    entered = entered_username.strip().lower()
    for approver in (
        get_active_tower_head(db),
        get_active_provisioning_approver(db),
    ):
        if approver and approver.email.strip().lower() == entered:
            return approver.email.strip().lower()
    return None


@router.get("/me")
def get_session(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> dict[str, Any]:
    is_approver = principal.claims.get("account_type") == APPROVER_ACCOUNT_TYPE
    return {
        "user": {
            "username": principal.claims["preferred_username"],
            "name": (
                principal.claims["preferred_username"]
                if is_approver
                else "Shared development account"
            ),
            "roles": [],
            "is_shared_account": not is_approver,
            "account_type": principal.claims.get("account_type"),
            "license_request_test_mode": (
                not is_approver
                and bool(principal.claims.get("license_request_test_mode"))
                and local_license_request_test_mode_enabled()
                and is_loopback_request(request)
            ),
        }
    }


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/api/v1",
        secure=os.getenv("AUTH_COOKIE_SECURE", "false").strip().lower()
        in {"true", "1", "yes"},
        httponly=True,
        samesite="strict",
    )
    return {"status": "signed out"}
