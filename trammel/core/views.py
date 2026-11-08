from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from trammel.accounts.models import User

from . import audit, modules, numbering
from .forms import (
    BusinessProfileForm,
    DocumentSequenceForm,
    SetupForm,
    ShopRatesForm,
    SystemSettingsForm,
)
from .models import AuditEvent, BusinessProfile, DocumentSequence, ShopRates, SystemSettings
from .navigation import menu_for
from .web import (
    admin_required,
    csv_response,
    document_response,
    document_view,
    paginate,
    permission_required,
)


@require_GET
def healthz(request):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


# ---------------------------------------------------------------------------
# Setup (FR-ADM-1)
# ---------------------------------------------------------------------------


def setup(request):
    """Runs once on an empty database. Afterwards it does not exist (404)."""
    if SystemSettings.objects.filter(setup_completed_at__isnull=False).exists():
        raise Http404
    token_required = bool(settings.SETUP_TOKEN)
    form = SetupForm(request.POST or None, token_required=token_required)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if token_required and data["setup_token"] != settings.SETUP_TOKEN:
            form.add_error("setup_token", "That is not this instance's setup token.")
        else:
            try:
                admin = _complete_setup(data)
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                login(request, admin, backend="django.contrib.auth.backends.ModelBackend")
                messages.success(request, "Setup complete. Welcome to Trammel.")
                return redirect("core:dashboard")
    return render(
        request,
        "core/setup.html",
        {"form": form, "secure": request.is_secure(), "base_url": settings.BASE_URL},
    )


@transaction.atomic
def _complete_setup(data) -> User:
    # Lock the settings row so two browsers submitting at once cannot both
    # create an admin.
    SystemSettings.load()
    system = SystemSettings.objects.select_for_update().get(pk=1)
    if system.setup_completed_at is not None or User.objects.exists():
        raise ValidationError("Setup has already been completed.")
    admin = User.objects.create_admin(
        email=data["email"], password=data["password"], full_name=data["full_name"]
    )
    business = BusinessProfile.load()
    business.name = data["business_name"]
    business.timezone = data["timezone"]
    business.unit_system = data["unit_system"]
    business.currency = data["currency"]
    business.full_clean()
    business.save()
    ShopRates.load()
    system.setup_completed_at = timezone.now()
    system.save()
    with audit.acting_as(admin, source="web"):
        audit.record(
            "setup.complete",
            business,
            summary=f"Completed setup for {business.name}",
            after={
                "admin": admin.email,
                "timezone": business.timezone,
                "currency": business.currency,
            },
        )
    return admin


# ---------------------------------------------------------------------------
# Dashboard and settings
# ---------------------------------------------------------------------------


@login_required
def dashboard(request):
    return render(request, "core/dashboard.html")


@login_required
def search(request):
    """Global search (FR-SRCH-1)."""
    from .search import run

    term = request.GET.get("q", "")
    return render(request, "core/search.html", {"q": term, "results": run(request.actor, term)})


@login_required
def settings_index(request):
    items = menu_for(request.actor, "admin")
    if not items:
        raise Http404
    return redirect(items[0].url)


def _settings_form_view(request, *, form_class, instance, template, title, success, **form_kwargs):
    before = audit.snapshot(instance)
    form = form_class(request.POST or None, instance=instance, **form_kwargs)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit.record_update(instance, before, summary=f"Edited {title.lower()}")
        messages.success(request, success)
        return redirect(request.path)
    return render(request, template, {"form": form, "title": title})


@permission_required("core.manage_business")
def business(request):
    return _settings_form_view(
        request,
        form_class=BusinessProfileForm,
        instance=BusinessProfile.load(),
        template="core/settings_form.html",
        title="Business",
        success="Business details saved.",
        currency_locked=modules.currency_locked(),
    )


@permission_required("core.manage_business")
def rates(request):
    return _settings_form_view(
        request,
        form_class=ShopRatesForm,
        instance=ShopRates.load(),
        template="core/settings_form.html",
        title="Shop rates",
        success="Shop rates saved. New quotes will use them; existing quotes keep theirs.",
    )


@admin_required
def system(request):
    return _settings_form_view(
        request,
        form_class=SystemSettingsForm,
        instance=SystemSettings.load(),
        template="core/system.html",
        title="System",
        success="System settings saved.",
    )


@permission_required("core.manage_business")
def sequences(request):
    rows = []
    for seq in DocumentSequence.objects.all():
        rows.append({"seq": seq, "next": numbering.preview_next(seq.key)})
    return render(request, "core/sequences.html", {"rows": rows})


@permission_required("core.manage_business")
def sequence_edit(request, key):
    seq = get_object_or_404(DocumentSequence, key=key)
    before = audit.snapshot(seq)
    form = DocumentSequenceForm(request.POST or None, instance=seq)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit.record_update(seq, before, summary=f"Changed numbering for {seq.label}")
        messages.success(request, f"{seq.label} numbering saved.")
        return redirect("core:sequences")
    return render(
        request,
        "core/settings_form.html",
        {"form": form, "title": f"{seq.label} numbering", "back": "core:sequences"},
    )


# ---------------------------------------------------------------------------
# Modules (FR-ADM-7)
# ---------------------------------------------------------------------------


@admin_required
def module_list(request):
    enabled = modules.enabled_keys()
    rows = [
        {
            "m": m,
            "enabled": m.key in enabled,
            "dependents": [d.name for d in modules.dependents_of(m.key, enabled)],
            "depends_on": [modules.get_manifest(d).name for d in m.depends_on],
        }
        for m in modules.manifests()
    ]
    return render(request, "core/modules.html", {"rows": rows})


@admin_required
@require_POST
def module_toggle(request, key):
    enable = request.POST.get("enable") == "1"
    try:
        modules.set_enabled(key, enable, reason=request.POST.get("reason", ""))
    except LookupError:
        raise Http404 from None
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        name = modules.get_manifest(key).name
        messages.success(request, f"{name} {'enabled' if enable else 'disabled'}.")
    return redirect("core:modules")


# ---------------------------------------------------------------------------
# Audit log (FR-AUD-3)
# ---------------------------------------------------------------------------


def _filtered_audit(request):
    qs = AuditEvent.objects.select_related("user")
    f = request.GET
    if f.get("action"):
        qs = qs.filter(action=f["action"])
    if f.get("entity_type"):
        qs = qs.filter(entity_type=f["entity_type"])
    if f.get("entity_id"):
        qs = qs.filter(entity_id=f["entity_id"])
    if f.get("user"):
        qs = qs.filter(user_id=f["user"])
    if f.get("actor_kind"):
        qs = qs.filter(actor_kind=f["actor_kind"])
    if f.get("since"):
        qs = qs.filter(at__date__gte=f["since"])
    if f.get("until"):
        qs = qs.filter(at__date__lte=f["until"])
    if f.get("q"):
        qs = qs.filter(summary__icontains=f["q"])
    return qs


@permission_required("core.view_audit")
def audit_log(request):
    try:
        qs = _filtered_audit(request)
        page = paginate(request, qs, 100)
    except ValidationError:
        messages.error(request, "One of the filters is not valid.")
        page = paginate(request, AuditEvent.objects.none())
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "core/audit.html",
        {
            "page": page,
            "filters": request.GET,
            "query": params.urlencode(),
            "actions": AuditEvent.objects.order_by("action")
            .values_list("action", flat=True)
            .distinct(),
            "entity_types": AuditEvent.objects.exclude(entity_type="")
            .order_by("entity_type")
            .values_list("entity_type", flat=True)
            .distinct(),
            "users": User.objects.all(),
        },
    )


@permission_required("core.view_audit")
def audit_csv(request) -> HttpResponse:
    qs = _filtered_audit(request)
    audit.record("export", entity_type="core.auditevent", summary="Exported the audit log as CSV")
    rows = (
        (
            e.at.isoformat(),
            e.actor_kind,
            e.actor_label,
            e.action,
            e.entity_type,
            e.entity_id,
            e.entity_label,
            e.summary,
            e.reason,
            e.ip or "",
            e.source,
        )
        for e in qs.iterator()
    )
    return csv_response(
        "audit-log.csv",
        [
            "at",
            "actor_kind",
            "actor",
            "action",
            "entity_type",
            "entity_id",
            "entity",
            "summary",
            "reason",
            "ip",
            "source",
        ],
        rows,
    )


# ---------------------------------------------------------------------------
# Template preview and placeholder reference
# ---------------------------------------------------------------------------


def _kind_or_404(request, key):
    from . import placeholders

    kind = placeholders.get(key)
    if kind is None or kind not in placeholders.kinds():
        raise Http404
    if not placeholders.allowed(request.actor, kind):
        raise PermissionDenied
    return kind


@login_required
@require_POST
@document_view
def template_preview(request, key):
    """Render the editor's unsaved text with a real or sample record."""

    from . import templating

    kind = _kind_or_404(request, key)
    ctx, source = kind.preview()
    try:
        body = templating.render(request.POST.get("body", ""), ctx, html=kind.html)
        subject = (
            templating.render(request.POST.get("subject", ""), ctx, html=False).strip()
            if kind.has_subject
            else ""
        )
    except templating.TemplateProblem as exc:
        return document_response(
            request,
            render_to_string(
                "core/template_preview_text.html",
                {"problem": str(exc), "source": source},
            ),
        )
    if kind.html:
        return document_response(request, body)
    return document_response(
        request,
        render_to_string(
            "core/template_preview_text.html",
            {"subject": subject, "text": body, "source": source, "has_subject": kind.has_subject},
        ),
    )


@login_required
def template_reference(request):
    from . import placeholders

    sections = [
        {
            "kind": kind,
            "reference": placeholders.reference(kind, _safe_preview(kind)),
        }
        for kind in placeholders.kinds()
        if placeholders.allowed(request.actor, kind)
    ]
    if not sections:
        raise PermissionDenied
    return render(
        request,
        "core/template_reference.html",
        {"sections": sections, "filters": placeholders.FILTERS, "logic": placeholders.LOGIC},
    )


def _safe_preview(kind):
    try:
        return kind.preview()[0]
    except Exception:  # noqa: BLE001
        return None
