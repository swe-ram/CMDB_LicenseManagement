import hmac
import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field


SESSION_COOKIE_NAME = "licensehub_session"
SESSION_ISSUER = "licensehub"
SESSION_AUDIENCE = "licensehub-frontend"
SHARED_ACCOUNT_SUBJECT = "shared-development-account"
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

    if claims.get("sub") != SHARED_ACCOUNT_SUBJECT or claims.get("account_type") != "shared":
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
        object_id=SHARED_ACCOUNT_SUBJECT,
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
) -> dict[str, Any]:
    username, password, signing_secret, lifetime_minutes, secure_cookie = _auth_settings()
    valid_username = hmac.compare_digest(
        credentials.username.encode("utf-8"),
        username.encode("utf-8"),
    )
    valid_password = hmac.compare_digest(
        credentials.password.encode("utf-8"),
        password.encode("utf-8"),
    )
    if not (valid_username and valid_password):
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
    token = jwt.encode(
        {
            "sub": SHARED_ACCOUNT_SUBJECT,
            "account_type": "shared",
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
    return {
        "user": {
            "username": username,
            "name": "Shared development account",
            "roles": [],
            "is_shared_account": True,
            "license_request_test_mode": request_test_mode,
        }
    }


@router.get("/me")
def get_session(
    request: Request,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
) -> dict[str, Any]:
    return {
        "user": {
            "username": principal.claims["preferred_username"],
            "name": "Shared development account",
            "roles": [],
            "is_shared_account": True,
            "license_request_test_mode": (
                bool(principal.claims.get("license_request_test_mode"))
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
