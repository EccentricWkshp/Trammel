from django.urls import reverse
from django.utils import timezone

from trammel.core import access, dashboard, panels
from trammel.jobs import hooks
from trammel.quotes import services as quotes

from . import services
from .models import OPEN, Invoice, Status

quotes.on_approved.append(services.create_deposit_invoice)


@hooks.check("closed")
def invoices_settled(job):
    """Job closeout needs every invoice paid, voided or written off (§6.1)."""
    for invoice in job.invoices.exclude(kind=Invoice.Kind.CREDIT_NOTE):
        if invoice.status == Status.DRAFT:
            yield f"A draft {invoice.get_kind_display().lower()} has not been issued or deleted."
        elif invoice.status in OPEN:
            yield f"{invoice.number} has {invoice.balance} outstanding."


@hooks.register_outstanding
def unpaid_balances(job):
    """§6.2: unpaid balances are written off or left outstanding, per invoice."""
    for invoice in job.invoices.filter(status__in=OPEN).exclude(kind=Invoice.Kind.CREDIT_NOTE):

        def resolve(choice, invoice=invoice):
            if choice == "write_off":
                services.write_off(invoice, reason="Job force-closed", from_force_closeout=True)

        yield hooks.Outstanding(
            key=f"invoice:{invoice.pk}",
            label=f"{invoice.number}: {invoice.balance} unpaid",
            choices=[("leave", "Leave it outstanding"), ("write_off", "Write it off")],
            resolve=resolve,
            detail={"invoice": invoice.number, "balance": str(invoice.balance)},
        )
    for draft in job.invoices.filter(status=Status.DRAFT):

        def delete(choice, draft=draft):
            if choice == "delete":
                draft.delete()

        yield hooks.Outstanding(
            key=f"draft:{draft.pk}",
            label=f"Unissued draft {draft.get_kind_display().lower()}",
            choices=[("delete", "Delete the draft"), ("leave", "Leave it")],
            resolve=delete,
        )


@panels.register("jobs.job")
def job_invoices(request, job):
    if not access.has_perm(request.actor, "billing.view"):
        return None
    invoices = access.scoped(request.actor, "billing.view", job.invoices.all()).order_by(
        "created_at"
    )
    return panels.Panel(
        "Invoices",
        "billing/_job_panel.html",
        {
            "invoices": invoices,
            "can_create": access.has_perm(request.actor, "billing.create", job)
            and not job.customer.is_internal,
            "internal": job.customer.is_internal,
        },
        order=30,
        tab="invoices",
        count=invoices.count(),
    )


@panels.register("customers.customer")
def customer_invoices(request, customer):
    if not access.has_perm(request.actor, "billing.view"):
        return None
    invoices = list(
        access.scoped(
            request.actor, "billing.view", customer.invoices.exclude(status=Status.DRAFT)
        ).order_by("-invoice_date")[:50]
    )
    return panels.Panel(
        "Invoices and payments",
        "billing/_customer_panel.html",
        {
            "invoices": invoices,
            "balance": services.customer_balance(customer),
            "credit": services.customer_credit(customer),
            "can_pay": access.has_perm(request.actor, "billing.record_payments"),
            "can_sell": access.has_perm(request.actor, "billing.create"),
        },
        order=30,
        tab="invoices",
        count=len(invoices),
    )


@dashboard.register(
    "billing.overdue",
    "Overdue invoices",
    order=50,
    permission="billing.view",
    description="Issued invoices past their due date, oldest first.",
)
def overdue_invoices(request):
    qs = access.scoped(
        request.actor,
        "billing.view",
        Invoice.objects.filter(status__in=OPEN, due_date__lt=timezone.localdate()).exclude(
            kind=Invoice.Kind.CREDIT_NOTE
        ),
    )
    rows = list(qs.select_related("customer").order_by("due_date")[: dashboard.LIMIT])
    return dashboard.Section(
        template="billing/_home_invoices.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("billing:list") + "?status=overdue",
        empty="Nothing is overdue.",
    )


@dashboard.register(
    "billing.drafts",
    "Unissued invoices",
    order=60,
    permission="billing.view",
    description="Draft invoices that have not been issued.",
)
def draft_invoices(request):
    qs = access.scoped(request.actor, "billing.view", Invoice.objects.filter(status=Status.DRAFT))
    rows = list(qs.select_related("customer").order_by("created_at")[: dashboard.LIMIT])
    return dashboard.Section(
        template="billing/_home_invoices.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("billing:list") + "?status=draft",
        empty="No draft invoices.",
    )
