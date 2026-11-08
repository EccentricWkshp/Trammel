from trammel.core import access, panels
from trammel.jobs import hooks

from .models import QuoteRevision, Status


@hooks.check("quoted")
def needs_a_sent_quote(job):
    if not QuoteRevision.objects.filter(
        quote__job=job, status__in=[Status.SENT, Status.APPROVED]
    ).exists():
        yield "Send a quote first; the job becomes quoted when one is sent."


@hooks.check("approved")
def needs_an_approved_quote(job):
    if not QuoteRevision.objects.filter(quote__job=job, status=Status.APPROVED).exists():
        yield "Approve a quote first; the job becomes approved when one is approved."


@hooks.register_duplicate
def copy_quote_lines(source, new):
    """FR-JOB-5: duplicating a job copies its quote lines into a new draft quote."""
    from . import services

    approved = (
        QuoteRevision.objects.filter(quote__job=source, status=Status.APPROVED).first()
        or QuoteRevision.objects.filter(quote__job=source).order_by("-created_at").first()
    )
    if approved is None:
        return
    revision = services.create_quote(new, title=approved.quote.title)
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
    latest = {}
    for rev in _revisions(request, QuoteRevision.objects.filter(quote__job=job)).order_by(
        "quote__created_at", "-number"
    ):
        latest.setdefault(rev.quote_id, rev)
    return panels.Panel(
        "Quotes",
        "quotes/_job_panel.html",
        {
            "revisions": list(latest.values()),
            "can_create": access.has_perm(request.actor, "quotes.create", job),
        },
        order=10,
        tab="quotes",
        count=len(latest),
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
