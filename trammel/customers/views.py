from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from trammel.core import access, audit, panels
from trammel.core.web import csv_response, paginate, permission_required

from . import services
from .forms import AddressForm, ContactForm, CustomerForm
from .models import Address, Contact, Customer


def _customers(request):
    qs = Customer.objects.all()
    if term := request.GET.get("q", "").strip():
        qs = qs.filter(Customer.search_filter(term))
    return qs


@permission_required("customers.view")
def customer_list(request):
    qs = access.scoped(request.actor, "customers.view", _customers(request))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "customers/list.html",
        {"page": paginate(request, qs), "q": request.GET.get("q", ""), "query": params.urlencode()},
    )


@permission_required("customers.view")
def customer_csv(request):
    qs = access.scoped(request.actor, "customers.view", _customers(request))
    audit.record("export", entity_type="customers.customer", summary="Exported customers as CSV")
    rows = (
        (
            c.name,
            c.company,
            c.get_kind_display(),
            c.email,
            c.phone,
            c.payment_terms_days,
            c.tax_exempt,
            c.tax_exempt_number,
            c.tax_exempt_expires,
        )
        for c in qs.iterator()
    )
    return csv_response(
        "customers.csv",
        [
            "name",
            "company",
            "kind",
            "email",
            "phone",
            "payment_terms_days",
            "tax_exempt",
            "tax_exempt_number",
            "tax_exempt_expires",
        ],
        rows,
    )


@permission_required("customers.view")
def customer_detail(request, pk):
    customer = access.get_or_404(request.actor, "customers.view", Customer.objects.all(), pk=pk)
    return render(
        request,
        "customers/detail.html",
        {
            "customer": customer,
            "contacts": customer.contacts.all(),
            "addresses": customer.addresses.all(),
            "terms": customer.effective_payment_terms(request.business),
            "can_edit": access.has_perm(request.actor, "customers.edit", customer),
            "can_delete": access.has_perm(request.actor, "customers.delete", customer)
            and not customer.is_placeholder,
            **panels.tabbed(
                request, panels.for_object(request, customer), last=(("files", "Files"),)
            ),
        },
    )


@permission_required("customers.create")
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        customer = services.save_customer(form.save(commit=False), before=None, actor=request.actor)
        messages.success(request, f"{customer} added.")
        return redirect(customer)
    return render(request, "customers/form.html", {"form": form, "title": "New customer"})


@permission_required("customers.edit")
def customer_edit(request, pk):
    customer = access.get_or_404(request.actor, "customers.edit", Customer.objects.all(), pk=pk)
    before = audit.snapshot(customer)
    form = CustomerForm(request.POST or None, instance=customer)
    if request.method == "POST" and form.is_valid():
        services.save_customer(form.save(commit=False), before=before)
        messages.success(request, f"{customer} saved.")
        return redirect(customer)
    return render(
        request,
        "customers/form.html",
        {"form": form, "title": f"Edit {customer}", "customer": customer},
    )


@permission_required("customers.delete")
def customer_delete(request, pk):
    customer = access.get_or_404(request.actor, "customers.delete", Customer.objects.all(), pk=pk)
    if request.method == "POST":
        try:
            services.delete_customer(customer, reason=request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(customer)
        messages.success(request, f"{customer} moved to the trash. It can be restored for 30 days.")
        return redirect("customers:list")
    return render(request, "customers/confirm_delete.html", {"customer": customer})


# ---------------------------------------------------------------------------
# Contacts and addresses
# ---------------------------------------------------------------------------

_CHILDREN = {
    "contact": (Contact, ContactForm, "contacts"),
    "address": (Address, AddressForm, "addresses"),
}


@permission_required("customers.edit")
def child_edit(request, pk, kind, child_pk=None):
    if kind not in _CHILDREN:
        raise Http404
    model, form_class, related = _CHILDREN[kind]
    customer = access.get_or_404(request.actor, "customers.edit", Customer.objects.all(), pk=pk)
    if child_pk:
        child = access.get_or_404(
            request.actor, "customers.edit", getattr(customer, related).all(), pk=child_pk
        )
        before = audit.snapshot(child)
    else:
        child, before = model(customer=customer), None
    form = form_class(request.POST or None, instance=child)
    if request.method == "POST" and form.is_valid():
        save = services.save_contact if kind == "contact" else services.save_address
        save(form.save(commit=False), before=before)
        messages.success(request, f"{model._meta.verbose_name.capitalize()} saved.")
        return redirect(customer)
    title = f"{'Edit' if child_pk else 'Add'} {model._meta.verbose_name} — {customer}"
    return render(
        request, "customers/form.html", {"form": form, "title": title, "customer": customer}
    )


@permission_required("customers.edit")
@require_POST
def child_delete(request, pk, kind, child_pk):
    if kind not in _CHILDREN:
        raise Http404
    model, _form, related = _CHILDREN[kind]
    customer = access.get_or_404(request.actor, "customers.edit", Customer.objects.all(), pk=pk)
    child = access.get_or_404(
        request.actor, "customers.edit", getattr(customer, related).all(), pk=child_pk
    )
    services.delete_child(child)
    messages.success(request, f"{model._meta.verbose_name.capitalize()} removed.")
    return redirect(customer)
