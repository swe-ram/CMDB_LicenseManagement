"""Build and send the approval-workflow notification emails.

Each function returns the :class:`EmailResult` from the underlying
:class:`~services.email_service.EmailService` so callers can surface delivery
status without ever falsely reporting success. The frontend link opens the
portal's License Requests page; the approver must still authenticate before a
decision can be made (the link never grants approval by itself).
"""

from __future__ import annotations

import html

from services.email_service import EmailResult, get_email_service


def _escape(value: str | None) -> str:
    return html.escape(value or "")


def _frontend_base_url() -> str:
    # Imported lazily so tests can control the environment.
    import os

    return (os.getenv("FRONTEND_BASE_URL") or "http://localhost:3000").rstrip("/")


def _request_link(request_id: str) -> str:
    return f"{_frontend_base_url()}/license-requests?request={request_id}"


def _summary_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th style='text-align:left;padding:6px 12px;background:#f3f4f6;"
        f"border:1px solid #e5e7eb;'>{_escape(label)}</th>"
        f"<td style='padding:6px 12px;border:1px solid #e5e7eb;'>{value}</td></tr>"
        for label, value in rows
    )
    return (
        "<table style='border-collapse:collapse;font-family:Arial,sans-serif;"
        f"font-size:14px;color:#111827;'>{body}</table>"
    )


def _wrap_email(title: str, intro: str, body_html: str, link: str | None = None) -> str:
    link_html = (
        f"<p style='margin:20px 0;'><a href='{link}' style='background:#2563eb;"
        f"color:#ffffff;padding:10px 18px;border-radius:6px;text-decoration:none;"
        f"font-family:Arial,sans-serif;'>View request in LicenseHub</a></p>"
        if link
        else ""
    )
    return (
        "<div style='font-family:Arial,sans-serif;color:#111827;max-width:640px;"
        "margin:0 auto;'>"
        f"<h2 style='color:#111827;'>{title}</h2>"
        f"<p style='font-size:14px;line-height:1.5;'>{intro}</p>"
        f"{body_html}"
        f"{link_html}"
        "<p style='font-size:12px;color:#6b7280;margin-top:24px;'>"
        "You must sign in to LicenseHub to review and decide this request. "
        "This link does not grant approval by itself.</p>"
        "</div>"
    )


def notify_tower_head_approval_request(
    *,
    request_id: str,
    employee_name: str,
    employee_email: str | None,
    department: str,
    application_name: str,
    license_name: str | None,
    business_justification: str,
    urgency: str,
    approver_email: str,
    approver_name: str,
) -> EmailResult:
    """Email the Tower Head that a request is awaiting their approval."""
    link = _request_link(request_id)
    rows = [
        ("Request ID", _escape(request_id)),
        ("Employee", f"{_escape(employee_name)}"
                     + (f" ({_escape(employee_email)})" if employee_email else "")),
        ("Department", _escape(department)),
        ("Application", _escape(application_name)),
        ("License", _escape(license_name or "—")),
        ("Urgency", _escape(urgency)),
        ("Business justification", _escape(business_justification)),
    ]
    html_body = _wrap_email(
        title="License request awaiting Tower Head approval",
        intro=(
            f"Hello {_escape(approver_name)}, a new license request has been "
            "submitted and is awaiting your approval as Tower Head."
        ),
        body_html=_summary_table(rows),
        link=link,
    )
    text_body = (
        f"License request {request_id} is awaiting your Tower Head approval.\n"
        f"Employee: {employee_name}"
        + (f" ({employee_email})" if employee_email else "")
        + f"\nDepartment: {department}\nApplication: {application_name}\n"
        f"License: {license_name or '—'}\nUrgency: {urgency}\n"
        f"Justification: {business_justification}\n"
        f"View request: {link}\n"
    )
    return get_email_service().send_email(
        to_addresses=[approver_email],
        subject=f"[LicenseHub] Approval needed: license request {request_id}",
        html_body=html_body,
        text_body=text_body,
    )


def notify_provisioning_approval_request(
    *,
    request_id: str,
    employee_name: str,
    employee_email: str | None,
    department: str,
    application_name: str,
    license_name: str | None,
    business_justification: str,
    urgency: str,
    tower_head_name: str,
    approver_email: str,
    approver_name: str,
) -> EmailResult:
    """Email the IT / License Provisioning approver after Tower Head approval."""
    link = _request_link(request_id)
    rows = [
        ("Request ID", _escape(request_id)),
        ("Employee", f"{_escape(employee_name)}"
                     + (f" ({_escape(employee_email)})" if employee_email else "")),
        ("Department", _escape(department)),
        ("Application", _escape(application_name)),
        ("License", _escape(license_name or "—")),
        ("Urgency", _escape(urgency)),
        ("Tower Head decision", f"Approved by {_escape(tower_head_name)}"),
        ("Business justification", _escape(business_justification)),
    ]
    html_body = _wrap_email(
        title="License request ready for IT / License Provisioning",
        intro=(
            f"Hello {_escape(approver_name)}, the Tower Head has approved license "
            "request below. It is now awaiting your IT / License Provisioning "
            "decision."
        ),
        body_html=_summary_table(rows),
        link=link,
    )
    text_body = (
        f"License request {request_id} was approved by Tower Head "
        f"{tower_head_name} and is awaiting your IT / License Provisioning "
        f"decision.\nEmployee: {employee_name}"
        + (f" ({employee_email})" if employee_email else "")
        + f"\nDepartment: {department}\nApplication: {application_name}\n"
        f"License: {license_name or '—'}\n"
        f"Justification: {business_justification}\nView request: {link}\n"
    )
    return get_email_service().send_email(
        to_addresses=[approver_email],
        subject=(
            f"[LicenseHub] Provisioning decision needed: license request {request_id}"
        ),
        html_body=html_body,
        text_body=text_body,
    )


def notify_requester_decision(
    *,
    request_id: str,
    requester_email: str | None,
    decision: str,
    stage_label: str,
    approver_name: str,
    comment: str | None,
) -> EmailResult:
    """Notify the requester of a final approval or rejection outcome."""
    if not requester_email:
        return EmailResult(
            delivered=False,
            skipped=True,
            error="Requester email is not available.",
        )
    link = _request_link(request_id)
    approved = decision.lower() == "approved"
    title = (
        "Your license request was approved"
        if approved
        else "Your license request was rejected"
    )
    rows = [
        ("Request ID", _escape(request_id)),
        ("Stage", _escape(stage_label)),
        ("Decision", _escape(decision)),
        ("Decided by", _escape(approver_name)),
    ]
    if comment:
        rows.append(("Comment", _escape(comment)))
    intro = (
        f"License request {request_id} has been {decision.lower()} at the "
        f"{stage_label} stage."
    )
    html_body = _wrap_email(title=title, intro=intro, body_html=_summary_table(rows), link=link)
    text_body = (
        f"{title}.\nRequest ID: {request_id}\nStage: {stage_label}\n"
        f"Decision: {decision}\nDecided by: {approver_name}\n"
        + (f"Comment: {comment}\n" if comment else "")
        + f"View request: {link}\n"
    )
    return get_email_service().send_email(
        to_addresses=[requester_email],
        subject=f"[LicenseHub] {title}: {request_id}",
        html_body=html_body,
        text_body=text_body,
    )

