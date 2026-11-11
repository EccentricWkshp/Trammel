from __future__ import annotations

import hmac

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

from . import audit, modules, numbering, panels
from .forms import (
    BusinessProfileForm,
    DocumentSequenceForm,
    SetupForm,
    ShopRatesForm,
    SystemSettingsForm,
)
from .middleware import client_ip
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
    return JsonResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Setup (FR-ADM-1)
# ---------------------------------------------------------------------------


def setup(request):
    """Runs once on an empty database. Afterwards it does not exist (404)."""
    if SystemSettings.objects.filter(setup_completed_at__isnull=False).exists():
        raise Http404
    token_required = bool(settings.SETUP_TOKEN)
    if not token_required and not _on_local_network(client_ip(request)):
        # Whoever completes setup becomes the admin; from the internet that must
        # not be just whoever arrives first.
        return render(request, "core/setup_remote.html", status=403)
    form = SetupForm(request.POST or None, token_required=token_required)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if token_required and not hmac.compare_digest(
            data["setup_token"].encode(), settings.SETUP_TOKEN.encode()
        ):
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


#: Networks that are someone's own: home and office ranges (RFC 1918), IPv6
#: unique-local addresses, and Docker's own bridge networks among them.
_LOCAL_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")


def _on_local_network(ip: str | None) -> bool:
    import ipaddress

    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback or address.is_link_local:
        return True
    return any(
        address.version == net.version and address in net
        for net in map(ipaddress.ip_network, _LOCAL_NETWORKS)
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
    from . import dashboard as home

    return render(request, "core/dashboard.html", {"sections": home.for_user(request)})


@login_required
def home_customize(request):
    """Choose which home screen sections to show, and in what order."""
    from . import dashboard as home

    arranged = home.arranged(request)
    if request.method == "POST":
        if "reset" in request.POST:
            home.save_prefs(request.user, [], set())
            messages.success(request, "Your home screen is back to the defaults.")
        else:
            keys = [d.key for d, _ in arranged]

            def place(key):
                try:
                    return (int(request.POST.get(f"order-{key}", "")), keys.index(key))
                except ValueError:
                    return (len(keys) + 1, keys.index(key))

            on = {k for k in keys if request.POST.get(f"on-{k}")}
            home.save_prefs(request.user, sorted(keys, key=place), on)
            messages.success(request, "Home screen saved.")
        return redirect("core:dashboard")
    return render(
        request,
        "core/home_customize.html",
        {"sections": [(d, on, n) for n, (d, on) in enumerate(arranged, start=1)]},
    )


@login_required
def search(request):
    """Global search (FR-SRCH-1)."""
    from .search import run

    term = request.GET.get("q", "")
    return render(request, "core/search.html", {"q": term, "results": run(request.actor, term)})


@login_required
def scan_open(request, pk):
    """A label's short link: open whatever record it was printed for (FR-STK-7)."""
    from . import scan

    url = scan.open_record(request, pk)
    if url is None:
        raise Http404
    return redirect(url)


@login_required
def settings_index(request):
    items = menu_for(request.actor, "admin")
    if not items:
        raise Http404
    return redirect(items[0].url)


def _settings_form_view(
    request, *, form_class, instance, template, title, success, context=None, **form_kwargs
):
    before = audit.snapshot(instance)
    form = form_class(request.POST or None, request.FILES or None, instance=instance, **form_kwargs)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit.record_update(instance, before, summary=f"Edited {title.lower()}")
        messages.success(request, success)
        return redirect(request.path)
    return render(request, template, {"form": form, "title": title, **(context or {})})


@permission_required("core.manage_business")
def business(request):
    """Business settings, one tab (and one form) at a time."""
    instance = BusinessProfile.load()
    tab = request.GET.get("tab", "")
    if tab not in BusinessProfileForm.tab_keys():
        tab = BusinessProfileForm.tab_keys()[0]
    before = audit.snapshot(instance)
    form = BusinessProfileForm(
        request.POST or None,
        request.FILES or None,
        instance=instance,
        tab=tab,
        currency_locked=modules.currency_locked(),
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            audit.record_update(instance, before, summary="Edited business")
        messages.success(request, "Business details saved.")
        return redirect(f"{request.path}?tab={tab}")
    return render(
        request,
        "core/business_form.html",
        {
            "form": form,
            "title": "Business",
            "tab": tab,
            "tabs": [
                {"key": key, "label": label, "active": key == tab}
                for key, label, _ in BusinessProfileForm.TABS
            ],
            # Modules add to a tab, such as the resale certificate's file on Tax.
            "panels": [p for p in panels.for_object(request, instance) if p.tab == tab],
        },
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
    instance = SystemSettings.load()
    return _settings_form_view(
        request,
        form_class=SystemSettingsForm,
        instance=instance,
        template="core/system.html",
        title="System",
        success="System settings saved.",
        # Modules add sections here, such as the update status.
        context={"panels": panels.for_object(request, instance), "portal": _portal_state()},
    )


def _portal_state() -> dict | None:
    """The customer portal's switch, shown on the System page whether the portal
    is on or off (FR-PRT-8), so it can always be found and turned off."""
    try:
        manifest = modules.get_manifest("portal")
    except LookupError:
        return None
    state = {
        "name": manifest.name,
        "on": modules.is_enabled("portal"),
        "forced_off": "portal" in modules.forced_off(),
    }
    if state["on"]:
        # Whether customers can send files, and how large (FR-PRT-7).
        from trammel.portal.forms import UploadSettingsForm
        from trammel.portal.models import PortalSettings

        state["upload_form"] = UploadSettingsForm(instance=PortalSettings.load())
    return state


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
    return redirect("core:system" if request.POST.get("back") == "system" else "core:modules")


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
