"""What the edge portal shows (FR-PRT-2 to -4, -10), as records to publish: the
shop's settings, each customer whose access is on, and their jobs, quotes,
invoices, agreements, shared notes and files, with their documents rendered
here (pricing, tax and templates stay in Trammel) and stored on Cloudflare.

Every item has a cheap fingerprint of what it's made from; the sync builds and
pushes an item only when that changes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from django.contrib.staticfiles import finders
from django.utils import timezone

from trammel.core import modules

#: As Trammel's own portal shows them (portal.views).
SHOWN_QUOTES = ("sent", "approved", "declined", "expired")
SHOWN_INVOICES = ("issued", "partially_paid", "paid")


@dataclass
class Item:
    type: str
    id: str
    fingerprint: str
    build: Callable[[], tuple[dict, dict]]
    customer: str | None = None
    job: str | None = None
    sort: str = ""


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()


def portal_open() -> bool:
    """Customers may use the edge portal: the portal and this module both on."""
    return modules.is_enabled("portal") and modules.is_enabled("edge")


def _d(value) -> str:
    from trammel.core.templatetags.trammel import d

    return d(value) if value else ""


def _money(value) -> str:
    from trammel.core.templatetags.trammel import money

    return money(value)


def _size(n) -> str:
    from django.template.defaultfilters import filesizeformat

    return filesizeformat(n) if n else ""


# ---------------------------------------------------------------------------
# The shop
# ---------------------------------------------------------------------------


def _asset(name: str, prefix: str, suffix: str) -> tuple[str, bytes]:
    path = finders.find(name)
    data = open(path, "rb").read() if path else b""  # noqa: SIM115, PTH123
    return f"{prefix}-{hashlib.sha256(data).hexdigest()[:12]}{suffix}", data


def _css() -> tuple[str, bytes]:
    return _asset("trammel.css", "portal", ".css")


def _uploads() -> dict:
    from trammel.portal import uploads

    if not uploads.is_on():
        return {"enabled": False}
    return {
        "enabled": True,
        "limit_mb": uploads.limit_mb(),
        "per_send": uploads.PER_SEND,
        "kinds_label": uploads.KINDS_LABEL,
    }


def _hooks() -> dict:
    """The online stores the Worker takes webhooks and callbacks for."""
    if not modules.is_enabled("imports"):
        return {}
    from trammel.imports import shopify
    from trammel.imports.models import EbaySettings, EtsySettings, ShopifySettings

    shop = ShopifySettings.load()
    ebay = EbaySettings.load()
    return {
        "shopify": bool(shop.enabled and shop.webhooks_enabled and shopify.webhook_secret(shop)),
        "etsy": EtsySettings.load().enabled,
        "ebay": ebay.enabled,
        "ebay_deletion": ebay.enabled,
    }


def _code_email() -> dict:
    """The sign-in code email, rendered with placeholders the Worker fills in."""
    from trammel.core import templating
    from trammel.core.models import BusinessProfile
    from trammel.notifications.services import template_for

    tpl = template_for("portal_code")
    ctx = {
        "business": templating.business_context(BusinessProfile.load()),
        "code": "%%CODE%%",
        "minutes": "%%MINUTES%%",
    }
    try:
        subject = templating.render(tpl.subject, ctx, html=False).strip()
        body = templating.render(tpl.body, ctx, html=False)
    except templating.TemplateProblem:
        subject, body = (
            "Your sign-in code",
            "Your code is %%CODE%%. It expires in %%MINUTES%% minutes.\n",
        )
    return {"enabled": tpl.enabled, "subject": subject, "body": body}


def _turnstile() -> str:
    from .models import EdgeSettings

    return EdgeSettings.load().turnstile_sitekey


#: A job in one of these, untouched for the days set, comes off Cloudflare.
CLOSED = ("closed", "declined", "cancelled")


def hidden_jobs(customer) -> set:
    """The customer's jobs no longer published: closed (or declined, canceled)
    and untouched for the days kept (0.9.2)."""
    import datetime as dt

    from trammel.jobs.models import Job

    from .models import EdgeSettings

    days = EdgeSettings.load().keep_closed_days
    cutoff = timezone.now() - dt.timedelta(days=days)
    return set(
        Job.objects.filter(
            customer=customer, status__in=CLOSED, updated_at__lte=cutoff
        ).values_list("pk", flat=True)
    )


def site_item() -> Item:
    from django.conf import settings

    from trammel.agreements.models import AgreementSettings
    from trammel.core.logo import data_uri
    from trammel.core.models import BusinessProfile

    business = BusinessProfile.load()
    css_name, css = _css()
    js_name, js = _asset("signature.js", "signature", ".js")
    data = {
        "portal_on": portal_open(),
        "css": css_name,
        "signature_js": js_name,
        "signing": AgreementSettings.load().signature_enabled,
        "uploads": _uploads(),
        "hooks": _hooks(),
        # Where the owner's browser goes on to, from a store's sign-in.
        "local_url": settings.BASE_URL.rstrip("/"),
        "turnstile": _turnstile(),
        "business": {
            "name": business.name,
            "logo": data_uri(business) if business.show_logo else "",
            "phone": business.phone if business.show_phone else "",
            "email": business.email if business.show_email else "",
        },
        "email": _code_email(),
    }

    def build():
        from . import client

        if not data["portal_on"]:
            # Closed (FR-PRT-8): everyone signed out at once.
            client.end_sessions()
        return data, {
            f"assets/{css_name}": (css, "text/css"),
            f"assets/{js_name}": (js, "text/javascript"),
        }

    return Item("config", "site", fingerprint(data), build)


# ---------------------------------------------------------------------------
# Customers and what they see
# ---------------------------------------------------------------------------


def customers():
    from trammel.customers.models import Customer

    return Customer.objects.filter(
        portal_access__enabled=True, is_placeholder=False, is_internal=False
    ).order_by("name")


def customer_item(customer) -> Item:
    from trammel.portal.services import signers_of

    data = {
        "name": customer.name,
        "signers": [{"name": n, "email": e} for n, e in signers_of(customer)],
    }
    return Item(
        "customer",
        str(customer.pk),
        fingerprint(data),
        lambda: (data, {}),
        customer=str(customer.pk),
    )


def job_items(customer) -> Iterator[Item]:
    from trammel.jobs.models import Job
    from trammel.portal.views import JOB_STATUS

    for job in Job.objects.filter(customer=customer).order_by("-created_at")[:200]:
        data = {
            "number": job.number,
            "title": job.title,
            "status": JOB_STATUS.get(job.status, job.get_status_display()),
            "promised": _d(job.promised_date),
            "due": _d(job.due_date),
        }
        yield Item(
            "job",
            str(job.pk),
            fingerprint(data),
            lambda data=data: (data, {}),
            customer=str(customer.pk),
            sort=job.created_at.isoformat(),
        )


def _pdf(html: str) -> bytes | None:
    from trammel.core import templating

    try:
        return templating.render_pdf(html)
    except templating.PdfUnavailable:
        return None


def quote_items(customer, site_fp: str) -> Iterator[Item]:
    from trammel.quotes import services as quotes
    from trammel.quotes.models import Approval, QuoteRevision

    revisions = QuoteRevision.objects.filter(
        quote__job__customer=customer, status__in=SHOWN_QUOTES
    ).select_related("quote__job")
    from .models import EdgeAnswer

    for rev in revisions:
        approval = Approval.objects.filter(revision=rev).first()
        open_ = quotes.can_answer(rev)
        answering = _answering(rev) if open_ else None
        # An answer refused here (the latest), by id: a second refusal worded the
        # same still replaces "received" on the customer's page.
        refused = (
            EdgeAnswer.objects.filter(revision=rev)
            .order_by("-at", "-pk")
            .values_list("pk", "message")
            .first()
            if rev.status in ("sent", "expired", "declined") and not approval
            else None
        )
        problem = refused[1] if refused else None
        source = fingerprint(
            str(rev.pk),
            rev.status,
            rev.updated_at,
            approval and approval.pk,
            open_,
            answering,
            refused,
            site_fp,
        )

        def build(
            rev=rev,
            approval=approval,
            open_=open_,
            source=source,
            answering=answering,
            problem=problem,
        ):
            key = f"doc/quote/{rev.pk}/{source[:12]}"
            html = quotes.render_html(rev)
            objects = {f"{key}.html": (html.encode(), "text/html; charset=utf-8")}
            pdf = _pdf(html)
            if pdf:
                objects[f"{key}.pdf"] = (pdf, "application/pdf")
            data = {
                "kind": rev.quote.get_kind_display(),
                "noun": rev.quote.noun,
                "number": rev.quote.number,
                "revision": rev.number,
                "title": rev.quote.title,
                "total": _money(rev.total),
                "status": rev.get_status_display(),
                "open": open_,
                "valid_until": _d(rev.valid_until),
                "job_number": rev.quote.job.number,
                "doc": f"{key}.html",
                "pdf": f"{key}.pdf" if pdf else "",
                "pdf_name": f"{rev.quote.number}-r{rev.number}.pdf",
                "approval": {
                    "by": approval.approved_by_name,
                    "at": _d(timezone.localtime(approval.approved_at)),
                }
                if approval
                else None,
                "respond": answering,
                "problem": problem or "",
            }
            return data, objects

        yield Item(
            "quote",
            str(rev.pk),
            source,
            build,
            customer=str(customer.pk),
            job=str(rev.quote.job_id),
            sort=(rev.sent_at or rev.created_at).isoformat(),
        )


#: Totals are worked out for every combination of choices up to this many.
MAX_COMBINATIONS = 64


def _answering(rev) -> dict:
    """What answering a quote takes (FR-PRT-3, FR-QUO-11): the terms, whether to
    sign, the choices it offers, and the total for each combination of them."""
    import itertools

    from trammel.agreements.models import AgreementSettings
    from trammel.quotes import services as quotes

    offered = quotes.choices(rev)
    groups = [
        {
            "name": g["name"],
            "default": str(g["chosen"].pk) if g["chosen"] else str(g["lines"][0].pk),
            "lines": [
                {
                    "id": str(line.pk),
                    "description": line.description,
                    "price": _money(line.line_total),
                }
                for line in g["lines"]
            ],
        }
        for g in offered["groups"]
    ]
    options = [
        {"id": str(line.pk), "description": line.description, "price": _money(line.line_total)}
        for line in offered["options"]
    ]
    combos = [[line["id"] for line in g["lines"]] for g in groups]
    subsets = [
        list(chosen)
        for n in range(len(options) + 1)
        for chosen in itertools.combinations([o["id"] for o in options], n)
    ]
    totals = {}
    count = len(subsets) * math.prod(len(c) for c in combos)
    if count <= MAX_COMBINATIONS:
        for picks in itertools.product(*combos) if combos else [()]:
            for extra in subsets:
                chosen = [*picks, *extra]
                figures = quotes.totals_with(rev, quotes.picked(rev, chosen))
                deposit = rev.deposit_for(figures["total"])
                totals[",".join(sorted(chosen))] = {
                    "total": _money(figures["total"]),
                    "deposit": _money(deposit) if deposit else "",
                }
    else:
        default = [g["default"] for g in groups]
        totals[",".join(sorted(default))] = {
            "total": _money(rev.total),
            "deposit": _money(rev.deposit_amount) if rev.deposit_amount else "",
        }
    return {
        "agreement": quotes.terms_to_accept(rev),
        "signing": AgreementSettings.load().signature_enabled,
        "choices": {"groups": groups, "options": options},
        "totals": totals,
    }


def upload_items(customer) -> Iterator[Item]:
    """Files the customer sent from the edge portal, for 90 days (FR-PRT-7)."""
    import datetime as dt

    from .models import EdgeUpload

    labels = {
        "pending": "Waiting for us to look at it",
        "accepted": "Accepted",
        "declined": "Not used",
    }
    since = timezone.now() - dt.timedelta(days=90)
    for row in EdgeUpload.objects.filter(customer=customer, at__gte=since).select_related("upload"):
        if row.upload is not None:
            status, reason = (
                labels.get(row.upload.status, row.upload.get_status_display()),
                row.upload.reason,
            )
        else:
            status, reason = "Not taken", row.reason
        data = {
            "name": row.name,
            "status": status,
            "reason": reason,
            "at": _d(timezone.localtime(row.at)),
        }
        yield Item(
            "upload",
            str(row.pk),
            fingerprint(data),
            lambda data=data: (data, {}),
            customer=str(customer.pk),
            job=str(row.job_id) if row.job_id else None,
            sort=row.at.isoformat(),
        )


def invoice_items(customer, site_fp: str) -> Iterator[Item]:
    if not modules.is_enabled("billing"):
        return
    from trammel.billing import services as billing
    from trammel.billing.models import Invoice

    for invoice in Invoice.objects.filter(customer=customer, status__in=SHOWN_INVOICES):
        source = fingerprint(
            str(invoice.pk), invoice.status, invoice.updated_at, invoice.balance, site_fp
        )

        def build(invoice=invoice, source=source):
            key = f"doc/invoice/{invoice.pk}/{source[:12]}"
            html = billing.render_html(invoice)
            objects = {f"{key}.html": (html.encode(), "text/html; charset=utf-8")}
            pdf = _pdf(html)
            if pdf:
                objects[f"{key}.pdf"] = (pdf, "application/pdf")
            data = {
                "kind": invoice.get_kind_display(),
                "number": invoice.number,
                "total": _money(invoice.total),
                "balance": _money(invoice.balance),
                "balance_due": invoice.balance > 0,
                "status": invoice.get_status_display(),
                "date": _d(invoice.invoice_date),
                "due": _d(invoice.due_date),
                "job_number": invoice.job.number if invoice.job_id else "",
                "doc": f"{key}.html",
                "pdf": f"{key}.pdf" if pdf else "",
                "pdf_name": f"{invoice.number}.pdf",
            }
            return data, objects

        yield Item(
            "invoice",
            str(invoice.pk),
            source,
            build,
            customer=str(customer.pk),
            job=str(invoice.job_id) if invoice.job_id else None,
            sort=(invoice.invoice_date or invoice.created_at).isoformat(),
        )


def agreement_items(customer, site_fp: str) -> Iterator[Item]:
    if not modules.is_enabled("agreements"):
        return
    from django.template.loader import render_to_string

    from trammel.agreements.models import CustomerAgreement
    from trammel.core.models import BusinessProfile

    for agreement in CustomerAgreement.objects.filter(
        job__customer=customer, status="accepted"
    ).select_related("job"):
        source = fingerprint(str(agreement.pk), agreement.updated_at, site_fp)

        def build(agreement=agreement, source=source):
            key = f"doc/agreement/{agreement.pk}/{source[:12]}.pdf"
            html = render_to_string(
                "agreements/print.html",
                {"agreement": agreement, "business": BusinessProfile.load()},
            )
            pdf = _pdf(html)
            data = {
                "title": "Customer agreement" if agreement.is_primary else "Additional terms",
                "accepted": _d(timezone.localtime(agreement.accepted_at))
                if agreement.accepted_at
                else "",
                "by": agreement.accepted_by_name,
                "pdf": key if pdf else "",
                "pdf_name": f"agreement-{agreement.job.number}.pdf",
            }
            return data, ({key: (pdf, "application/pdf")} if pdf else {})

        yield Item(
            "agreement",
            str(agreement.pk),
            source,
            build,
            customer=str(customer.pk),
            job=str(agreement.job_id),
            sort=(agreement.accepted_at or agreement.created_at).isoformat(),
        )


def _blob_bytes(blob) -> bytes:
    from trammel.files import services as files

    return files.blob_path(blob).read_bytes()


def _thumb(blob) -> tuple[str, bytes] | None:
    from trammel.files.services import media_root, thumbnail_mime

    if not blob.thumbnail:
        return None
    path = media_root() / blob.thumbnail
    return (thumbnail_mime(blob), path.read_bytes()) if path.exists() else None


def note_items(customer) -> Iterator[Item]:
    if not modules.is_enabled("workorders"):
        return
    from trammel.workorders.models import WorkOrderNote

    notes = WorkOrderNote.objects.filter(
        work_order__job__customer=customer, shared_with_customer=True
    ).select_related("author", "document__current__blob", "work_order")
    for note in notes:
        current = note.document.current if note.document_id else None
        source = fingerprint(str(note.pk), note.text, current and str(current.pk))

        def build(note=note, current=current):
            objects, photo = {}, ""
            if current is not None:
                photo = f"note/{note.pk}/{current.pk}"
                objects[photo] = (_blob_bytes(current.blob), current.blob.mime)
            data = {
                "text": note.text,
                "author": note.author.get_short_name() if note.author else "",
                "at": _d(timezone.localtime(note.created_at)),
                "photo": photo,
            }
            return data, objects

        yield Item(
            "note",
            str(note.pk),
            source,
            build,
            customer=str(customer.pk),
            job=str(note.work_order.job_id),
            sort=note.created_at.isoformat(),
        )


def file_items(customer) -> Iterator[Item]:
    if not modules.is_enabled("files"):
        return
    from django.contrib.contenttypes.models import ContentType

    from trammel.files.models import Attachment
    from trammel.jobs.models import Job

    targets = [(ContentType.objects.get_for_model(customer), customer.pk, None)]
    job_type = ContentType.objects.get_for_model(Job)
    targets += [
        (job_type, pk, str(pk))
        for pk in Job.objects.filter(customer=customer).values_list("pk", flat=True)
    ]
    for content_type, object_id, job in targets:
        links = Attachment.objects.filter(
            content_type=content_type,
            object_id=object_id,
            shared_with_customer=True,
            document__deleted_at__isnull=True,
            document__current__isnull=False,
        ).select_related("document__current__blob")
        for link in links:
            current = link.document.current
            source = fingerprint(
                str(link.pk), link.document.title, str(current.pk), current.blob.thumbnail
            )

            def build(link=link, current=current):
                key = f"file/{link.pk}/{current.pk}"
                objects = {key: (_blob_bytes(current.blob), current.blob.mime)}
                thumb = _thumb(current.blob)
                if thumb:
                    objects[f"{key}.thumb"] = (thumb[1], thumb[0])
                data = {
                    "title": link.document.title,
                    "name": current.original_name,
                    "size": _size(current.blob.size),
                    "key": key,
                    "thumb": f"{key}.thumb" if thumb else "",
                }
                return data, objects

            yield Item(
                "file",
                str(link.pk),
                source,
                build,
                customer=str(customer.pk),
                job=job,
                sort=link.created_at.isoformat(),
            )


def access_items(customer) -> Iterator[Item]:
    """Codes staff made that are still good (FR-PRT-10)."""
    from .models import EdgeAccessCode

    for row in EdgeAccessCode.objects.filter(
        code__customer=customer,
        code__used_at=None,
        code__canceled_at=None,
        code__expires_at__gt=timezone.now(),
    ).select_related("code__created_by"):
        code = row.code
        data = {
            "expires_at": code.expires_at.isoformat(),
            "given_by": code.created_by.display_name if code.created_by else "staff",
            "code_id": str(code.pk),
        }
        yield Item(
            "access",
            row.code_hash,
            fingerprint(data),
            lambda data=data: (data, {}),
            customer=str(customer.pk),
        )


def items() -> Iterator[Item]:
    """Everything the edge portal should hold now. Closed, just the settings."""
    site = site_item()
    yield site
    if not portal_open():
        return
    for customer in customers():
        hidden = {str(pk) for pk in hidden_jobs(customer)}
        yield customer_item(customer)
        for item in (
            *job_items(customer),
            *quote_items(customer, site.fingerprint),
            *invoice_items(customer, site.fingerprint),
            *agreement_items(customer, site.fingerprint),
            *note_items(customer),
            *file_items(customer),
            *upload_items(customer),
        ):
            # A closed job past keeping takes everything with it (0.9.2).
            if (item.id if item.type == "job" else item.job) not in hidden:
                yield item
        yield from access_items(customer)
