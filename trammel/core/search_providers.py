"""Searchers for the built-in modules (FR-SRCH-1). Each checks its module is present and
the actor may view what it returns, through the same record scoping as every list."""

from __future__ import annotations

from django.apps import apps
from django.urls import reverse

from . import access, modules
from .search import Hit, matching, register


def _installed(label: str) -> bool:
    """Installed and switched on: a disabled module's records stay out of search."""
    if not apps.is_installed(label):
        return False
    key = modules.module_of(label)
    return key is None or modules.is_enabled(key)


def _scoped(actor, perm, qs):
    return access.scoped(actor, perm, qs) if access.has_perm(actor, perm) else qs.none()


@register("Customers")
def customers(actor, term):
    from trammel.customers.models import Customer

    qs = matching(Customer.objects.all(), term, ("name", "company", "email", "phone"), ("notes",))
    for c in _scoped(actor, "customers.view", qs)[:10]:
        yield Hit("Customer", str(c), c.get_absolute_url(), c.email or c.phone)


@register("Jobs")
def jobs(actor, term):
    if not _installed("trammel.jobs"):
        return
    from trammel.jobs.models import Job

    qs = matching(
        Job.objects.select_related("customer"),
        term,
        ("number", "title", "customer__name"),
        ("description",),
    )
    for j in _scoped(actor, "jobs.view", qs).order_by("-created_at")[:10]:
        yield Hit(
            "Job",
            f"{j.number} {j.title}",
            j.get_absolute_url(),
            f"{j.customer.name} · {j.get_status_display()}",
        )


@register("Quotes")
def quotes(actor, term):
    if not _installed("trammel.quotes"):
        return
    from trammel.quotes.models import Quote

    qs = matching(
        Quote.objects.select_related("job__customer"), term, ("number", "title", "job__number")
    )
    for q in _scoped(actor, "quotes.view", qs).order_by("-created_at")[:10]:
        yield Hit("Quote", f"{q.number} {q.title}", q.get_absolute_url(), q.job.customer.name)


@register("Work orders")
def work_orders(actor, term):
    if not _installed("trammel.workorders"):
        return
    from trammel.workorders.models import WorkOrder

    qs = matching(
        WorkOrder.objects.select_related("job__customer"),
        term,
        ("number", "title", "job__number"),
        ("instructions",),
    )
    for w in _scoped(actor, "workorders.view", qs).order_by("-created_at")[:10]:
        yield Hit(
            "Work order", f"{w.number} {w.title}", w.get_absolute_url(), w.get_status_display()
        )


@register("Invoices")
def invoices(actor, term):
    if not _installed("trammel.billing"):
        return
    from trammel.billing.models import Invoice

    qs = matching(
        Invoice.objects.select_related("customer"),
        term,
        ("number", "customer__name", "job__number"),
    ).exclude(number="")
    for i in _scoped(actor, "billing.view", qs).order_by("-created_at")[:10]:
        yield Hit(
            "Invoice",
            i.number,
            i.get_absolute_url(),
            f"{i.customer.name} · {i.get_status_display()} · {i.total}",
        )


@register("Products")
def products(actor, term):
    if not _installed("trammel.catalog"):
        return
    from trammel.catalog.models import Product

    qs = matching(Product.objects.all(), term, ("name", "sku"), ("description",))
    for p in _scoped(actor, "catalog.view", qs)[:10]:
        yield Hit("Product", str(p), p.get_absolute_url(), p.get_kind_display())


@register("Materials")
def materials(actor, term):
    if not _installed("trammel.shop"):
        return
    from trammel.shop.models import Material

    qs = matching(Material.objects.all(), term, ("name", "sku", "vendor__name"))
    for m in _scoped(actor, "shop.view", qs)[:10]:
        yield Hit(
            "Material", str(m), reverse("shop:material_edit", args=[m.pk]), m.get_category_display()
        )
