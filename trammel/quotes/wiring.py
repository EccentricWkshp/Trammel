import datetime as dt

from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from trammel.core import access, dashboard, panels
from trammel.jobs import hooks

from .models import Quote, QuoteRevision, Status

QUOTE = Quote.Kind.QUOTE


@hooks.on_updated
def rush_fee(job, before):
    """Draft quotes gain or lose their rush fee as the job moves to or from Rush."""
    from . import services

    try:
        was = int(before.get("priority", -1)) == services.RUSH_PRIORITY
    except TypeError, ValueError:
        was = False
    services.rush_changed(job, was)


@hooks.check("quoted")
def needs_a_sent_quote(job):
    if not QuoteRevision.objects.filter(
        quote__job=job, quote__kind=QUOTE, status__in=[Status.SENT, Status.APPROVED]
    ).exists():
        yield "Send a quote first; the job becomes quoted when one is sent."


@hooks.check("approved")
def needs_an_approved_quote(job):
    if not QuoteRevision.objects.filter(
        quote__job=job, quote__kind=QUOTE, status=Status.APPROVED
    ).exists():
        yield "Approve a quote first; the job becomes approved when one is approved."


@hooks.register_duplicate
def copy_quote_lines(source, new):
    """FR-JOB-5: duplicating a job copies its quote lines into a new draft quote."""
    from . import services

    quotes = QuoteRevision.objects.filter(quote__job=source, quote__kind=QUOTE)
    approved = (
        quotes.filter(status=Status.APPROVED).first() or quotes.order_by("-created_at").first()
    )
    if approved is None:
        return
    revision = services.create_quote(new, title=approved.quote.title)
    revision.description = approved.description
    revision.save(update_fields=["description"])
    for line in approved.lines.all():
        line.pk = None
        line.revision = revision
        line.position = 0
        services.save_line(line)


def _revisions(request, qs):
    return access.scoped(request.actor, "quotes.view", qs).select_related("quote")


@panels.register("jobs.job")
def job_quotes(request, job):
    if not access.has_perm(request.actor, "quotes.view"):
        return None
    from . import services

    latest = {}
    for rev in _revisions(request, QuoteRevision.objects.filter(quote__job=job)).order_by(
        "quote__created_at", "-number"
    ):
        latest.setdefault(rev.quote_id, rev)
    revisions = list(latest.values())
    after_approval = services.is_after_approval(job)
    return panels.Panel(
        "Quotes",
        "quotes/_job_panel.html",
        {
            "revisions": [r for r in revisions if not r.quote.is_change_order],
            "change_orders": [r for r in revisions if r.quote.is_change_order],
            "after_approval": after_approval,
            "can_create": access.has_perm(request.actor, "quotes.create", job)
            and job.status not in ("closed", "declined", "cancelled"),
        },
        order=10,
        tab="quotes",
        count=len(revisions),
    )


@panels.register("customers.customer")
def customer_quotes(request, customer):
    if not access.has_perm(request.actor, "quotes.view"):
        return None
    revisions = (
        _revisions(request, QuoteRevision.objects.filter(quote__job__customer=customer))
        .exclude(status=Status.SUPERSEDED)
        .order_by("-created_at")[:50]
    )
    revisions = list(revisions)
    return panels.Panel(
        "Quotes",
        "quotes/_customer_panel.html",
        {"revisions": revisions},
        order=20,
        tab="quotes",
        count=len(revisions),
    )


@dashboard.register(
    "quotes.awaiting",
    "Quotes awaiting approval",
    order=30,
    permission="quotes.view",
    description="Sent quotes and change orders, those closest to expiring first.",
)
def quotes_awaiting(request):
    today = timezone.localdate()
    qs = access.scoped(
        request.actor, "quotes.view", QuoteRevision.objects.filter(status=Status.SENT)
    )
    ordered = qs.select_related("quote__job__customer").order_by(
        F("valid_until").asc(nulls_last=True), "created_at"
    )
    rows = list(ordered[: dashboard.LIMIT])
    return dashboard.Section(
        template="quotes/_home_awaiting.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("quotes:list") + "?status=sent",
        empty="No quotes are waiting on a customer.",
        context={"today": today, "soon": today + dt.timedelta(days=7)},
    )
