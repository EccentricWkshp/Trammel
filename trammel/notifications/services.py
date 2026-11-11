"""Sending email (FR-NTF-1). Everything goes through the task queue and is logged."""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string

from trammel.core import audit, secrets, tasks, templating
from trammel.core.models import BusinessProfile, SystemSettings

from .events import EVENTS
from .models import EmailLog, EmailTemplate, SmtpSettings

log = logging.getLogger("trammel.notifications")

PASSWORD_KEY = "smtp.password"


def template_for(event: str) -> EmailTemplate:
    spec = EVENTS[event]
    tpl, _ = EmailTemplate.objects.get_or_create(
        event=event, defaults={"subject": spec.subject, "body": spec.body}
    )
    return tpl


def staff_recipients() -> list[str]:
    from trammel.accounts.models import User

    chosen = SmtpSettings.load().staff_recipients.filter(is_active=True).exclude(email="")
    users = chosen if chosen.exists() else User.objects.filter(is_admin=True, is_active=True)
    return list(users.exclude(email="").values_list("email", flat=True))


def connection():
    """The SMTP connection from settings — or a test backend (TRAMMEL_EMAIL_BACKEND)."""
    if backend := getattr(settings, "TRAMMEL_EMAIL_BACKEND", ""):
        return import_string(backend)()
    from django.core.mail.backends.smtp import EmailBackend

    smtp = SmtpSettings.load()
    # Named (alias), so Django takes every option from here. Without a name it
    # falls back to the old EMAIL_* settings for anything not given, and those
    # raise once MAILERS is defined (Django 6.1).
    return EmailBackend(
        alias="trammel-smtp",
        host=smtp.host,
        port=smtp.port,
        username=smtp.username or None,
        password=secrets.get(PASSWORD_KEY) or None,
        use_tls=smtp.security == SmtpSettings.Security.STARTTLS,
        use_ssl=smtp.security == SmtpSettings.Security.SSL,
        timeout=15,
    )


def _from() -> str:
    smtp = SmtpSettings.load()
    return f"{smtp.from_name} <{smtp.from_address}>" if smtp.from_name else smtp.from_address


#: Callables that narrow the invoices overdue reminders go to. A module whose
#: sales are collected elsewhere (an online store) leaves its own out.
reminder_filters: list = []


def notify(
    event: str,
    *,
    to: list[str] | None = None,
    context: dict | None = None,
    attachment: dict | None = None,
    entity=None,
) -> EmailLog | None:
    """Render and queue one email. Returns its log row (None if nobody to send to).

    `attachment` names a document the worker renders to PDF:
    {"kind": "quote" | "invoice", "id": "<uuid>"}.
    """
    spec = EVENTS[event]
    recipients = [
        a
        for a in (
            to if to is not None else (staff_recipients() if spec.audience == "staff" else [])
        )
        if a
    ]
    if not recipients:
        return None
    tpl = template_for(event)
    ctx = {"business": templating.business_context(BusinessProfile.load()), **(context or {})}
    try:
        subject = templating.render(tpl.subject, ctx, html=False).strip()
        body = templating.render(tpl.body, ctx, html=False)
    except templating.TemplateProblem as exc:
        subject, body = spec.subject, ""
        status, detail = EmailLog.Status.FAILED, f"Template problem: {exc}"
    else:
        status, detail = EmailLog.Status.QUEUED, ""
    smtp = SmtpSettings.load()
    if status == EmailLog.Status.QUEUED:
        if not tpl.enabled:
            status, detail = EmailLog.Status.SKIPPED, "This notification is turned off."
        elif SystemSettings.load().is_offline:
            status, detail = EmailLog.Status.SKIPPED, "Outbound traffic is switched off."
        elif not smtp.is_configured and not getattr(settings, "TRAMMEL_EMAIL_BACKEND", ""):
            status, detail = EmailLog.Status.SKIPPED, "Email (SMTP) is not configured."
    entry = EmailLog.objects.create(
        event=event,
        to=", ".join(recipients),
        subject=subject[:300],
        status=status,
        detail=detail,
        entity_type=entity._meta.label_lower if entity is not None else "",
        entity_id=str(entity.pk) if entity is not None else "",
    )
    if status == EmailLog.Status.QUEUED:
        payload = {
            "log": str(entry.pk),
            "to": recipients,
            "subject": subject,
            "body": body,
            "attachment": attachment,
        }
        # A sign-in link or code is cleared from the queue once sent.
        payload["secret"] = event in SECRET_EVENTS
        transaction.on_commit(lambda: tasks.enqueue("notifications.send", payload))
    return entry


#: Emails carrying a way to sign in: their text isn't kept once they're sent.
SECRET_EVENTS = frozenset({"magic_link", "portal_code"})


def send_now(payload: dict) -> None:
    """Deliver a queued email. Called by the worker."""
    from django.core.mail import EmailMessage

    entry = EmailLog.objects.filter(pk=payload["log"]).first()
    message = EmailMessage(
        subject=payload["subject"],
        body=payload["body"],
        from_email=_from() or settings.DEFAULT_FROM_EMAIL,
        to=payload["to"],
    )
    if spec := payload.get("attachment"):
        name, data = _render_attachment(spec)
        if data:
            message.attach(name, data, "application/pdf")
    try:
        connection().send_messages([message])
    except Exception as exc:
        if entry:
            EmailLog.objects.filter(pk=entry.pk).update(
                status=EmailLog.Status.FAILED, detail=str(exc)[:2000]
            )
        raise
    if entry:
        EmailLog.objects.filter(pk=entry.pk).update(status=EmailLog.Status.SENT, detail="")


def _render_attachment(spec: dict) -> tuple[str, bytes | None]:
    try:
        if spec["kind"] == "quote":
            from trammel.quotes.models import QuoteRevision
            from trammel.quotes.services import render_pdf

            rev = QuoteRevision.objects.get(pk=spec["id"])
            return f"{rev.quote.number}.pdf", render_pdf(rev)
        if spec["kind"] == "invoice":
            from trammel.billing.models import Invoice
            from trammel.billing.services import render_html

            inv = Invoice.objects.get(pk=spec["id"])
            return f"{inv.number}.pdf", templating.render_pdf(render_html(inv))
    except templating.PdfUnavailable:
        log.warning("PDF output unavailable; sending without an attachment.")
    return "", None


def send_test(to: str, *, user=None) -> None:
    """Send immediately, so the settings screen can report success or the error."""
    from django.core.mail import EmailMessage

    message = EmailMessage(
        subject="Trammel test email",
        body="This is a test email from Trammel. Email is working.",
        from_email=_from(),
        to=[to],
    )
    # The SMTP server comes from the database, not MAILERS, so the backend is
    # built here and handed the message directly.
    connection().send_messages([message])
    audit.record(
        "smtp.test",
        entity_type="notifications.smtpsettings",
        summary=f"Sent a test email to {to}",
        actor=user,
    )
