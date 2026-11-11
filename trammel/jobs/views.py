from __future__ import annotations

import calendar
import datetime as dt

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from trammel.core import access, agenda, audit, panels, sorting
from trammel.core.lifecycle import TransitionError
from trammel.core.models import AuditEvent
from trammel.core.web import csv_response, paginate, permission_required

from . import services
from .forms import JobForm, TransitionForm
from .models import OPEN_STATES, TERMINAL, Job, Priority, Status

# ---------------------------------------------------------------------------
# Lists and calendar (FR-JOB-9)
# ---------------------------------------------------------------------------


SORTS = sorting.Sorts(
    {
        "due": sorting.By("Due date", "due_date", "-created_at"),
        "priority": sorting.By("Priority, then due date", "-priority", "due_date"),
        "number": sorting.By("Job number", "number"),
        "customer": sorting.By("Customer", "customer__name"),
        "status": sorting.By("Status", sorting.in_order("status", Status.values)),
        "created": sorting.By("Date created", "created_at", newest_first=True),
    },
    default="due",
)


def _filtered(request):
    qs = Job.objects.select_related("customer")
    status = request.GET.get("status", "open")
    if status == "open":
        qs = qs.filter(status__in=OPEN_STATES)
    elif status in Status.values:
        qs = qs.filter(status=status)
    if term := request.GET.get("q", "").strip():
        qs = qs.filter(
            Q(number__icontains=term)
            | Q(title__icontains=term)
            | Q(customer__name__icontains=term)
            | Q(customer__company__icontains=term)
        )
    if tag := request.GET.get("tag", "").strip().lower():
        qs = qs.filter(tags__contains=[tag])
    priority = request.GET.get("priority", "")
    if priority == "high":
        qs = qs.filter(priority__gte=Priority.HIGH)
    elif priority.isdigit() and int(priority) in Priority.values:
        qs = qs.filter(priority=int(priority))
    qs, _ = SORTS.apply(request, qs)
    return access.scoped(request.actor, "jobs.view", qs), status


@permission_required("jobs.view")
def job_list(request):
    qs, status = _filtered(request)
    page = paginate(request, qs)
    today = timezone.localdate()
    rows = [(job, job.schedule_flags(today)) for job in page]
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "jobs/list.html",
        {
            "page": page,
            "rows": rows,
            "status": status,
            "statuses": Status.choices,
            "q": request.GET.get("q", ""),
            "tag": request.GET.get("tag", ""),
            "priority": request.GET.get("priority", ""),
            "priorities": [(str(v), label) for v, label in Priority.choices],
            "query": params.urlencode(),
            **SORTS.apply(request, Job.objects.none())[1],
        },
    )


@permission_required("jobs.view")
def job_csv(request):
    qs, _status = _filtered(request)
    audit.record("export", entity_type="jobs.job", summary="Exported jobs as CSV")
    rows = (
        (
            j.number,
            j.title,
            j.customer.name,
            j.get_status_display(),
            j.get_priority_display(),
            ",".join(j.tags),
            j.target_completion,
            j.due_date,
            j.promised_date,
            j.delivery_planned,
            j.delivery_actual,
            j.created_at.date(),
        )
        for j in qs.iterator()
    )
    return csv_response(
        "jobs.csv",
        [
            "number",
            "title",
            "customer",
            "status",
            "priority",
            "tags",
            "target_completion",
            "due",
            "promised",
            "delivery_planned",
            "delivery_actual",
            "created",
        ],
        rows,
    )


VIEWS = (("month", "Month"), ("week", "Week"), ("day", "Day"))
#: The week view shows Monday to Friday, or all seven days.
WEEK_STYLES = (("work", "Work week"), ("full", "7 days"))


def _anchor(request, today: dt.date) -> dt.date:
    """The day the calendar is showing: ?date=, the older ?month=, or today."""
    try:
        return dt.date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        pass
    try:
        return dt.date.fromisoformat(request.GET.get("month", "") + "-01")
    except ValueError:
        return today


def _calendar_entries(request, start: dt.date, end: dt.date, today: dt.date) -> list:
    """Jobs' dates and other modules' entries between start and end."""
    date_fields = [name for name, _label in Job.DATE_FIELDS] + ["delivery_actual"]
    span = Q()
    for name in date_fields:
        span |= Q(**{f"{name}__range": (start, end)})
    jobs = access.scoped(
        request.actor,
        "jobs.view",
        Job.objects.filter(span).exclude(status__in=[Status.DECLINED, Status.CANCELLED]),
    ).select_related("customer")

    labels = dict(Job.DATE_FIELDS) | {"delivery_actual": "Delivered"}
    out = []
    for job in jobs:
        finished = job.status in (Status.COMPLETED, Status.CLOSED)
        for name in date_fields:
            day = getattr(job, name)
            if day and start <= day <= end:
                done = finished or name == "delivery_actual"
                out.append(
                    agenda.Entry(
                        day=day,
                        label=labels[name],
                        text=f"{job.number} {job.title}",
                        url=job.get_absolute_url(),
                        # Late once the day has passed, not on the day itself.
                        late=job.is_open and not done and day < today,
                        kind="job",
                        done=done,
                    )
                )
    # Other modules' dates: deliveries expected on purchase orders, say.
    out += agenda.entries(request, start, end)
    return out


def _pinned(user) -> dict:
    """The calendar view this person made their default, if any."""
    return (getattr(user, "preferences", None) or {}).get("calendar") or {}


def _when(day: dt.date, today: dt.date) -> str:
    days = (day - today).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


@permission_required("jobs.view")
def job_calendar(request):
    """The calendar by month (the default), week or day, with its items listed
    below as a task list, soonest first (FR-JOB-9)."""
    today = timezone.localdate()
    pinned = _pinned(request.user)
    view = request.GET.get("view") or pinned.get("view", "month")
    if view not in dict(VIEWS):
        view = "month"
    # The week style sticks for the session, so they needn't pick it each time.
    style = (
        request.GET.get("week")
        or request.session.get("calendar_week")
        or pinned.get("week", "full")
    )
    if style not in dict(WEEK_STYLES):
        style = "full"
    request.session["calendar_week"] = style
    anchor = _anchor(request, today)
    week_start = request.business.week_start
    weekdays = [calendar.day_abbr[(week_start + i) % 7] for i in range(7)]

    if view == "month":
        first = anchor.replace(day=1)
        weeks = calendar.Calendar(firstweekday=week_start).monthdatescalendar(
            first.year, first.month
        )
        days = [day for week in weeks for day in week]
        prev = (first - dt.timedelta(days=1)).replace(day=1)
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
    elif view == "week":
        first = anchor - dt.timedelta(days=(anchor.weekday() - week_start) % 7)
        if style == "work":
            monday = first + dt.timedelta(days=(0 - week_start) % 7)
            days = [monday + dt.timedelta(days=i) for i in range(5)]
        else:
            days = [first + dt.timedelta(days=i) for i in range(7)]
        prev, nxt = anchor - dt.timedelta(days=7), anchor + dt.timedelta(days=7)
    else:
        days = [anchor]
        prev, nxt = anchor - dt.timedelta(days=1), anchor + dt.timedelta(days=1)

    start, end = days[0], days[-1]
    entries = _calendar_entries(request, start, end, today)
    by_day: dict[dt.date, list] = {}
    for entry in entries:
        by_day.setdefault(entry.day, []).append(entry)
    # Soonest first; on one day, what is still to do before what is done.
    tasks = sorted(entries, key=lambda e: (e.day, e.done, e.label, e.text))

    def link(**changes) -> str:
        params = {"view": view, "date": anchor.isoformat()} | changes
        if params["view"] == "week":
            params.setdefault("week", style)
        return "?" + "&".join(f"{k}={v}" for k, v in params.items())

    context = {
        "view": view,
        "views": [
            {"key": k, "label": label, "url": link(view=k), "active": k == view}
            for k, label in VIEWS
        ],
        "week_styles": [
            {"key": k, "label": label, "url": link(week=k), "active": k == style}
            for k, label in WEEK_STYLES
        ],
        "anchor": anchor,
        "today": today,
        "start": start,
        "end": end,
        "prev": link(date=prev.isoformat()),
        "next": link(date=nxt.isoformat()),
        "this": link(date=today.isoformat()),
        "tasks": [(e, _when(e.day, today)) for e in tasks],
        "has_others": agenda.any_for(request),
        "week_style": style,
        "is_default": pinned.get("view", "month") == view
        and (view != "week" or pinned.get("week", "full") == style),
    }
    if view == "month":
        context |= {
            "month": first,
            "weeks": [[(day, by_day.get(day, [])) for day in week] for week in weeks],
            "weekdays": weekdays,
        }
    else:
        context["days"] = [(day, by_day.get(day, [])) for day in days]
    return render(request, "jobs/calendar.html", context)


@require_POST
@permission_required("jobs.view")
def calendar_default(request):
    """Make the calendar view being shown this person's default."""
    view = request.POST.get("view", "")
    style = request.POST.get("week", "full")
    if view in dict(VIEWS) and style in dict(WEEK_STYLES):
        user = request.user
        user.preferences = (user.preferences or {}) | {"calendar": {"view": view, "week": style}}
        user.save(update_fields=["preferences"])
        request.session["calendar_week"] = style
        messages.success(request, "The calendar will open in this view from now on.")
    if view not in dict(VIEWS):
        return redirect("jobs:calendar")
    try:
        date = dt.date.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        date = timezone.localdate()
    return redirect(f"{reverse('jobs:calendar')}?view={view}&week={style}&date={date.isoformat()}")


# ---------------------------------------------------------------------------
# One job
# ---------------------------------------------------------------------------


def _get(request, perm, pk):
    return access.get_or_404(request.actor, perm, Job.objects.select_related("customer"), pk=pk)


@permission_required("jobs.view")
def job_detail(request, pk):
    job = _get(request, "jobs.view", pk)
    actor = request.actor
    actions = [t for t in Job.lifecycle.available(job, actor) if t.kind != "force"]
    timeline = AuditEvent.objects.filter(entity_type="jobs.job", entity_id=str(job.pk)).order_by(
        "-at"
    )[:100]
    return render(
        request,
        "jobs/detail.html",
        {
            "job": job,
            "flags": job.schedule_flags(timezone.localdate()),
            "actions": actions,
            "can_edit": access.has_perm(actor, "jobs.edit", job) and job.status not in TERMINAL,
            "can_create": access.has_perm(actor, "jobs.create"),
            "can_delete": access.has_perm(actor, "jobs.delete", job) and job.is_deletable,
            "can_force": job.status not in TERMINAL
            and Job.lifecycle.find(job, Status.CLOSED, "force") is not None
            and access.has_perm(actor, "jobs.force_closeout", job),
            "timeline": timeline,
            **panels.tabbed(
                request,
                panels.for_object(request, job),
                last=(("files", "Files"), ("history", "History")),
                obj=job,
            ),
        },
    )


@permission_required("jobs.create")
def job_create(request):
    initial = {}
    if customer_id := request.GET.get("customer"):
        initial["customer"] = customer_id
    form = JobForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        job = services.create_job(form.save(commit=False))
        messages.success(request, f"Job {job.number} created.")
        return redirect(job)
    return render(request, "jobs/form.html", {"form": form, "title": "New job"})


@permission_required("jobs.edit")
def job_edit(request, pk):
    job = _get(request, "jobs.edit", pk)
    before = audit.snapshot(job)
    form = JobForm(request.POST or None, instance=job)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_job(form.save(commit=False), before=before)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Job {job.number} saved.")
            return redirect(job)
    return render(
        request, "jobs/form.html", {"form": form, "title": f"Edit {job.number}", "job": job}
    )


@permission_required("jobs.view")
@require_POST
def job_transition(request, pk):
    job = _get(request, "jobs.view", pk)
    form = TransitionForm(request.POST)
    if form.is_valid():
        try:
            services.transition(
                job,
                form.cleaned_data["target"],
                actor=request.actor,
                reason=form.cleaned_data["reason"],
            )
        except TransitionError as exc:
            for problem in exc.problems:
                messages.error(request, problem)
        except PermissionDenied:
            messages.error(request, "Your role does not allow that.")
        else:
            job.refresh_from_db()
            messages.success(request, f"{job.number} is now {job.get_status_display().lower()}.")
    return redirect(job)


@permission_required("jobs.force_closeout")
def job_force_close(request, pk):
    job = _get(request, "jobs.force_closeout", pk)
    if job.status in TERMINAL:
        messages.error(request, f"{job.number} is already {job.get_status_display().lower()}.")
        return redirect(job)
    items = services.outstanding(job)
    errors = []
    if request.method == "POST":
        resolutions = {item.key: request.POST.get(f"resolve-{item.key}", "") for item in items}
        if request.POST.get("confirm") != "yes":
            errors.append("Tick the confirmation box to force the closeout.")
        else:
            try:
                services.force_close(
                    job,
                    actor=request.actor,
                    reason=request.POST.get("reason", ""),
                    resolutions=resolutions,
                )
            except TransitionError as exc:
                errors.extend(exc.problems)
            except ValidationError as exc:
                errors.extend(exc.messages)
            else:
                messages.warning(request, f"{job.number} was force-closed.")
                return redirect(job)
    from . import hooks

    return render(
        request,
        "jobs/force_close.html",
        {
            "job": job,
            "items": items,
            "failing": hooks.problems(job, Status.CLOSED),
            "errors": errors,
            "reason": request.POST.get("reason", ""),
        },
    )


@permission_required("jobs.create")
@require_POST
def job_duplicate(request, pk):
    source = _get(request, "jobs.view", pk)
    copy = services.duplicate_job(source)
    messages.success(request, f"Duplicated {source.number} as {copy.number}.")
    return redirect(copy)


@permission_required("jobs.delete")
def job_delete(request, pk):
    job = _get(request, "jobs.delete", pk)
    if request.method == "POST":
        try:
            services.delete_job(job, reason=request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(job)
        messages.success(request, f"{job.number} moved to the trash.")
        return redirect("jobs:list")
    return render(request, "jobs/confirm_delete.html", {"job": job})
