from datetime import timedelta

from django.utils import timezone

from trammel.core.tasks import periodic, task


@task("notifications.send")
def send(payload):
    from .services import send_now

    send_now(payload)


@periodic("notifications.overdue", every=timedelta(hours=24))
def overdue_reminders(payload):
    """Remind customers about overdue invoices, at most once per interval (FR-NTF-2)."""
    from trammel.billing.models import OPEN, Invoice
    from trammel.billing.services import document_context

    from .models import EmailLog, SmtpSettings
    from .services import notify, reminder_filters

    interval = timedelta(days=SmtpSettings.load().overdue_reminder_days or 7)
    due = Invoice.objects.filter(status__in=OPEN, due_date__lt=timezone.localdate()).exclude(
        kind=Invoice.Kind.CREDIT_NOTE
    )
    for narrow in reminder_filters:
        due = narrow(due)
    for invoice in due.select_related("customer"):
        if not invoice.customer.email:
            continue
        recent = EmailLog.objects.filter(
            event="invoice_overdue", entity_id=str(invoice.pk), at__gte=timezone.now() - interval
        ).exists()
        if recent:
            continue
        ctx = document_context(invoice)
        notify("invoice_overdue", to=[invoice.customer.email], context=ctx, entity=invoice)
