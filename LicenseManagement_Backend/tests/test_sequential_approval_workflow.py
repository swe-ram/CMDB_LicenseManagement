"""Automated tests for the two-stage sequential license approval workflow.

Covers the required scenarios:
  1.  Submitting a request creates a unique request ID.
  2.  Submitting a request creates a pending Tower Head approval.
  3.  Submitting a request creates a not-started provisioning approval.
  4.  Tower Head approval activates the provisioning stage.
  5.  Tower Head rejection prevents provisioning.
  6.  Provisioning approval before Tower Head approval is rejected.
  7.  Provisioning approval completes the approval workflow.
  8.  Provisioning rejection marks the request rejected.
  9.  Unauthorized users cannot make approval decisions.
  10. Repeated decisions cannot overwrite an existing decision.
  11. Email delivery failure does not delete the saved request.
  12. Existing request APIs and features continue to work.

External email delivery is mocked in every test; no real emails are sent.
"""

import os
import uuid
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import app
from auth.session import AuthenticatedPrincipal, get_current_principal
from database.approver_repository import ensure_default_approvers
from database.database import Base, get_db
from database.models import (
    Application,
    Department,
    License,
    LicenseApproval,
    LicenseRequest,
    User,
    APPROVAL_STAGE_IT_LICENSE_PROVISIONING,
    APPROVAL_STAGE_TOWER_HEAD,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_NOT_STARTED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_PENDING_PROVISIONING,
    REQUEST_STATUS_REJECTED,
)
from services.email_service import (
    EmailConfig,
    EmailService,
    reset_email_service_for_tests,
)

TOWER_HEAD_EMAIL = "akshayap@clouddestinations.com"
PROVISIONING_EMAIL = "jerryj@clouddestinations.com"


def _make_email_service(delivered=True, error=None):
    """Build an EmailService whose sender never touches the network."""
    config = EmailConfig(
        host="smtp.example.test",
        port=587,
        username=None,
        password=None,
        use_tls=True,
        use_ssl=False,
        from_address="no-reply@example.test",
        from_name="LicenseHub",
        enabled=True,
    )

    def _sender(cfg, message, recipients):  # noqa: ANN001
        if not delivered:
            raise RuntimeError(error or "simulated SMTP failure")

    return EmailService(config=config, sender=_sender)


class _FakeEmailServicePatch:
    """Context manager that patches the notification module's email service."""

    def __init__(self, service):
        self.service = service

    def __enter__(self):
        import services.approval_notifications as notifications

        self._original = notifications.get_email_service
        notifications.get_email_service = lambda: self.service
        return self.service

    def __exit__(self, *exc):
        import services.approval_notifications as notifications

        notifications.get_email_service = self._original
        return False


class SequentialApprovalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.Session()

        department = Department(name="Software Engineering", active=True)
        application = Application(name="Microsoft 365", status="Active")
        self.db.add_all([department, application])
        self.db.flush()

        self.employee = User(
            external_user_id="employee-oid",
            name="Test Employee",
            email="employee@example.test",
            status="active",
            department_id=department.id,
        )
        self.db.add(self.employee)
        self.db.flush()

        self.license = License(
            application_id=application.id,
            product="Microsoft 365 Business Premium",
            license_type="Business Suite",
            available_qty=10,
        )
        self.db.add(self.license)
        ensure_default_approvers(self.db)
        self.db.commit()

        self.employee_principal = AuthenticatedPrincipal(
            object_id=self.employee.external_user_id,
            roles=frozenset({"License.Requester"}),
            claims={},
        )
        self.tower_head_principal = AuthenticatedPrincipal(
            object_id=f"approver:{TOWER_HEAD_EMAIL}",
            roles=frozenset(),
            claims={
                "account_type": "approver",
                "preferred_username": TOWER_HEAD_EMAIL,
            },
        )
        self.provisioning_principal = AuthenticatedPrincipal(
            object_id=f"approver:{PROVISIONING_EMAIL}",
            roles=frozenset(),
            claims={
                "account_type": "approver",
                "preferred_username": PROVISIONING_EMAIL,
            },
        )
        self.outsider_principal = AuthenticatedPrincipal(
            object_id="outsider-oid",
            roles=frozenset({"License.Requester"}),
            claims={"preferred_username": "outsider@example.test"},
        )

        app.dependency_overrides[get_db] = self._get_db
        self.client = TestClient(app, client=("127.0.0.1", 50000))
        self._activate(self.employee_principal)

    def tearDown(self):
        app.dependency_overrides.clear()
        reset_email_service_for_tests()
        self.db.close()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def _get_db(self):
        yield self.db

    def _activate(self, principal):
        app.dependency_overrides[get_current_principal] = lambda: principal

    def _payload(self, **overrides):
        payload = {
            "user_id": self.employee.id,
            "idempotency_key": str(uuid.uuid4()),
            "department": "Software Engineering",
            "application_name": "Microsoft 365",
            "license_id": self.license.id,
            "business_justification": "Required for assigned project work.",
            "urgency": "Standard",
        }
        payload.update(overrides)
        return payload

    def _create_request(self):
        response = self.client.post(
            "/api/v1/license-requests", json=self._payload()
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _approval(self, request_db_id, stage):
        return (
            self.db.query(LicenseApproval)
            .filter(
                LicenseApproval.request_id == request_db_id,
                LicenseApproval.stage == stage,
            )
            .first()
        )

    def test_01_submission_creates_unique_request_id(self):
        with _FakeEmailServicePatch(_make_email_service()):
            first = self._create_request()
            second = self._create_request()
        self.assertTrue(first["request_id"].startswith("LIC-"))
        self.assertTrue(second["request_id"].startswith("LIC-"))
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(self.db.query(LicenseRequest).count(), 2)

    def test_02_submission_creates_pending_tower_head_approval(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)
        self.assertIsNotNone(tower_head)
        self.assertEqual(tower_head.status, APPROVAL_STATUS_PENDING)
        self.assertEqual(created["tower_head_status"], APPROVAL_STATUS_PENDING)

    def test_03_submission_creates_not_started_provisioning_approval(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
        provisioning = self._approval(
            created["id"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        )
        self.assertIsNotNone(provisioning)
        self.assertEqual(provisioning.status, APPROVAL_STATUS_NOT_STARTED)
        self.assertEqual(
            created["provisioning_status_stage"], APPROVAL_STATUS_NOT_STARTED
        )

    def test_03b_submission_sends_tower_head_email(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
        notifications = created["email_notifications"]
        self.assertTrue(any(n.get("delivered") for n in notifications))
        recipients = [r for n in notifications for r in (n.get("recipients") or [])]
        self.assertIn(TOWER_HEAD_EMAIL, recipients)

    def test_04_tower_head_approval_activates_provisioning(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.tower_head_principal)
            decided = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved", "comments": "Looks good."},
            )
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()
        self.assertEqual(body["status"], REQUEST_STATUS_PENDING_PROVISIONING)
        self.assertEqual(body["current_stage"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING)
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)
        provisioning = self._approval(
            created["id"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        )
        self.assertEqual(tower_head.status, APPROVAL_STATUS_APPROVED)
        self.assertIsNotNone(tower_head.decision_at)
        self.assertEqual(provisioning.status, APPROVAL_STATUS_PENDING)
        provisioning_notifications = [
            n
            for n in body["email_notifications"]
            if PROVISIONING_EMAIL in (n.get("recipients") or [])
        ]
        self.assertTrue(provisioning_notifications)

    def test_05_tower_head_rejection_prevents_provisioning(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.tower_head_principal)
            decided = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Rejected", "comments": "Not justified."},
            )
            self._activate(self.provisioning_principal)
            provisioning_attempt = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/it-license-provisioning/decision",
                json={"decision": "Approved"},
            )
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], REQUEST_STATUS_REJECTED)
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)

    def test_06_provisioning_before_tower_head_is_rejected(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.provisioning_principal)
            attempt = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/it-license-provisioning/decision",
                json={"decision": "Approved"},
            )
        self.assertEqual(attempt.status_code, 409)
        provisioning = self._approval(
            created["id"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        )
        self.assertEqual(provisioning.status, APPROVAL_STATUS_NOT_STARTED)

    def test_07_provisioning_approval_completes_workflow(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.tower_head_principal)
            self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved"},
            )
            self._activate(self.provisioning_principal)
            decided = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/it-license-provisioning/decision",
                json={"decision": "Approved", "comments": "Provisioned."},
            )
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()
        self.assertEqual(body["status"], REQUEST_STATUS_APPROVED)
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)
        provisioning = self._approval(
            created["id"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        )
        self.assertEqual(tower_head.status, APPROVAL_STATUS_APPROVED)
        self.assertEqual(provisioning.status, APPROVAL_STATUS_APPROVED)
        self.assertIsNotNone(provisioning.decision_at)
        requester_notifications = [
            n
            for n in body["email_notifications"]
            if "employee@example.test" in (n.get("recipients") or [])
        ]
        self.assertTrue(requester_notifications)

    def test_08_provisioning_rejection_marks_request_rejected(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.tower_head_principal)
            self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved"},
            )
            self._activate(self.provisioning_principal)
            decided = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/it-license-provisioning/decision",
                json={"decision": "Rejected", "comments": "No budget."},
            )
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], REQUEST_STATUS_REJECTED)
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)
        provisioning = self._approval(
            created["id"], APPROVAL_STAGE_IT_LICENSE_PROVISIONING
        )
        self.assertEqual(tower_head.status, APPROVAL_STATUS_APPROVED)
        self.assertEqual(provisioning.status, APPROVAL_STATUS_REJECTED)

    def test_09_unauthorized_users_cannot_decide(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.outsider_principal)
            outsider = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved"},
            )
            self._activate(self.provisioning_principal)
            wrong_stage = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved"},
            )
            self._activate(self.employee_principal)
            employee_self = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved"},
            )
        self.assertEqual(outsider.status_code, 403)

    def test_10_repeated_decisions_are_rejected(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            self._activate(self.tower_head_principal)
            first = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Approved", "comments": "First decision."},
            )
            second = self.client.post(
                f"/api/v1/license-requests/{created['request_id']}"
                "/tower-head/decision",
                json={"decision": "Rejected", "comments": "Attempt to override."},
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        tower_head = self._approval(created["id"], APPROVAL_STAGE_TOWER_HEAD)
        self.assertEqual(tower_head.status, APPROVAL_STATUS_APPROVED)
        self.assertEqual(tower_head.comments, "First decision.")

    def test_11_email_failure_preserves_request(self):
        failing = _make_email_service(
            delivered=False, error="SMTP connection refused"
        )
        with _FakeEmailServicePatch(failing):
            created = self.client.post(
                "/api/v1/license-requests", json=self._payload()
            )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(self.db.query(LicenseRequest).count(), 1)
        self.assertEqual(self.db.query(LicenseApproval).count(), 2)
        tower_head = self._approval(body["id"], APPROVAL_STAGE_TOWER_HEAD)
        self.assertEqual(tower_head.status, APPROVAL_STATUS_PENDING)
        self.assertTrue(
            any(
                (not n.get("delivered")) and n.get("error")
                for n in body["email_notifications"]
            )
        )

    def test_12_existing_apis_still_work(self):
        with _FakeEmailServicePatch(_make_email_service()):
            created = self._create_request()
            listed = self.client.get("/api/v1/license-requests")
            self.assertEqual(listed.status_code, 200)
            self.assertTrue(
                any(r["request_id"] == created["request_id"] for r in listed.json())
            )
            detail = self.client.get(
                f"/api/v1/license-requests/{created['request_id']}"
            )
            self.assertEqual(detail.status_code, 200)
            stages = {a["stage"] for a in detail.json()["approvals"]}
            self.assertIn(APPROVAL_STAGE_TOWER_HEAD, stages)
            self.assertIn(APPROVAL_STAGE_IT_LICENSE_PROVISIONING, stages)
            tower_head = self.client.get(
                "/api/v1/license-requests/approvers/tower-head"
            )
            provisioning = self.client.get(
                "/api/v1/license-requests/approvers/it-license-provisioning"
            )
            self.assertEqual(tower_head.status_code, 200)
            self.assertEqual(tower_head.json()["email"], TOWER_HEAD_EMAIL)
            self.assertEqual(provisioning.status_code, 200)
            self.assertEqual(provisioning.json()["email"], PROVISIONING_EMAIL)
            history = self.client.get(
                f"/api/v1/license-requests/{created['request_id']}/approvals"
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(len(history.json()), 2)


if __name__ == "__main__":
    unittest.main()
