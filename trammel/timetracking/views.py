from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from trammel.core import access, audit, sorting
from trammel.core.web import csv_response, paginate, permission_required, safe_next
from trammel.shop.models import Machine
from trammel.workorders.models import WorkOrder

from . import services
from .models import TimeEntry


class ManualEntryForm(forms.Form):
    kind = forms.ChoiceField(choices=TimeEntry.Kind.choices)
    machine = forms.ModelChoiceField(Machine.objects.filter(is_active=True), required=False)
    started_at = forms.DateTimeField(
        label="Started", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    minutes = forms.DecimalField(max_digits=9, decimal_places=2, min_value=1)
    billable = forms.BooleanField(required=False, initial=True)
    note = forms.CharField(required=False)

    def clean(self):
        data = super().clean()
        if data.get("kind") == "machine" and not data.get("machine"):
            self.add_error("machine", "Pick the machine.")
        return data


class CorrectionForm(forms.ModelForm):
    reason = forms.CharField(help_text="Why this entry is being corrected.")

    class Meta:
        model = TimeEntry
        fields = ["started_at", "ended_at", "minutes", "billable", "note"]
        widgets = {
            "started_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ended_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


def _wo(request, pk):
    return access.get_or_404(request.actor, "time.track", WorkOrder.objects.all(), pk=pk)


def _back(request, wo):
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("time.track")
@require_POST
def start(request, wo_pk):
    wo = _wo(request, wo_pk)
    kind = request.POST.get("kind", "labor")
    machine = Machine.objects.filter(pk=request.POST.get("machine") or None).first()
    try:
        services.start(wo, kind=kind, user=request.user, machine=machine)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return _back(request, wo)


@permission_required("time.track")
@require_POST
def stop(request, pk):
    entry = access.get_or_404(
        request.actor, "time.track", TimeEntry.objects.select_related("work_order"), pk=pk
    )
    if (
        entry.kind == "labor"
        and entry.user_id != request.user.pk
        and not access.has_perm(request.actor, "time.manage")
    ):
        raise Http404
    services.stop(entry)
    messages.success(request, f"Timer stopped: {entry.minutes} minutes.")
    return _back(request, entry.work_order)


@permission_required("time.track")
def manual(request, wo_pk):
    wo = _wo(request, wo_pk)
    form = ManualEntryForm(
        request.POST or None,
        initial={"started_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")},
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.manual(
                wo,
                kind=d["kind"],
                minutes=d["minutes"],
                started_at=d["started_at"],
                user=request.user,
                machine=d["machine"],
                billable=d["billable"],
                note=d["note"],
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect(wo)
    return render(
        request,
        "timetracking/form.html",
        {"form": form, "title": f"Add time to {wo.number}", "wo": wo},
    )


@permission_required("time.manage")
def correct(request, pk):
    entry = access.get_or_404(
        request.actor, "time.manage", TimeEntry.objects.select_related("work_order"), pk=pk
    )
    before = audit.snapshot(entry)
    form = CorrectionForm(request.POST or None, instance=entry)
    if request.method == "POST" and form.is_valid():
        try:
            services.correct(
                form.save(commit=False), before=before, reason=form.cleaned_data["reason"]
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect(entry.work_order)
    return render(
        request,
        "timetracking/form.html",
        {"form": form, "title": "Correct time entry", "wo": entry.work_order},
    )


def _filtered(request):
    qs = TimeEntry.objects.select_related("work_order__job", "user", "machine")
    f = request.GET
    if f.get("user"):
        qs = qs.filter(user_id=f["user"])
    if f.get("machine"):
        qs = qs.filter(machine_id=f["machine"])
    if f.get("since"):
        qs = qs.filter(started_at__date__gte=f["since"])
    if f.get("until"):
        qs = qs.filter(started_at__date__lte=f["until"])
    if f.get("job"):
        qs = qs.filter(work_order__job__number__icontains=f["job"])
    if not access.has_perm(request.actor, "time.manage"):
        qs = qs.filter(user=request.user)
    return access.scoped(request.actor, "time.view", qs)


SORTS = sorting.Sorts(
    {
        "started": sorting.By("Started", "started_at", newest_first=True),
        "work_order": sorting.By("Work order", "work_order__number", "-started_at"),
        "who": sorting.By("Who or machine", "user__full_name", "machine__name"),
        "minutes": sorting.By("Minutes", "minutes", newest_first=True),
        "cost": sorting.By("Cost", "cost", newest_first=True),
    },
    default="started",
)


def _hidden_sorts(actor) -> set[str]:
    return set() if access.has_perm(actor, "core.see_costs") else {"cost"}


@permission_required("time.view")
def entry_list(request):
    from trammel.accounts.models import User

    try:
        qs, _ = SORTS.apply(request, _filtered(request), hide=_hidden_sorts(request.actor))
        page = paginate(request, qs, 100)
    except ValidationError:
        messages.error(request, "One of the filters is not valid.")
        page = paginate(request, TimeEntry.objects.none())
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "timetracking/list.html",
        {
            "page": page,
            "filters": request.GET,
            "query": params.urlencode(),
            "users": User.objects.filter(is_active=True),
            "machines": Machine.objects.all(),
            "can_manage": access.has_perm(request.actor, "time.manage"),
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
            "running": services.running_for(request.user).select_related("work_order").first(),
            **SORTS.apply(request, TimeEntry.objects.none(), hide=_hidden_sorts(request.actor))[1],
        },
    )


@permission_required("time.view")
def entry_csv(request):
    audit.record("export", entity_type="timetracking.timeentry", summary="Exported time as CSV")
    rows = (
        (
            e.started_at.isoformat(),
            e.ended_at.isoformat() if e.ended_at else "",
            e.work_order.job.number,
            e.work_order.number,
            e.get_kind_display(),
            str(e.user or ""),
            str(e.machine or ""),
            e.minutes,
            e.billable,
            e.note,
        )
        for e in _filtered(request).iterator()
    )
    return csv_response(
        "time.csv",
        [
            "started",
            "ended",
            "job",
            "work_order",
            "kind",
            "user",
            "machine",
            "minutes",
            "billable",
            "note",
        ],
        rows,
    )
