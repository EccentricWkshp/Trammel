# No `from __future__ import annotations` here: list_events is wrapped by
# Ninja's paginate decorator, which resolves string annotations against Ninja's
# own module (see trammel/jobs/api.py).
"""The audit log (FR-AUD-3) over the API: who did what, to which record, when.
Read only, as everywhere: the log is append-only."""

import datetime as dt
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja import ModelSchema, Router
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.api.types import choices
from trammel.core import access

from .models import ActorKind, AuditEvent

router = Router(tags=["audit log"])


class AuditEventOut(ModelSchema):
    actor_kind: choices(ActorKind)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "at",
            "actor_kind",
            "user",
            "customer_id",
            "api_key_id",
            "actor_label",
            "action",
            "entity_type",
            "entity_id",
            "entity_label",
            "summary",
            "reason",
            "before",
            "after",
            "ip",
            "source",
        ]


@router.get("/", response=list[AuditEventOut])
@paginate(LimitOffsetPagination)
def list_events(
    request,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    user: UUID | None = None,
    actor_kind: choices(ActorKind) | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
    q: str | None = None,
):
    """Newest first, filtered as on the Audit log page; `q` searches the summaries."""
    access.require(request.actor, "core.view_audit")
    qs = AuditEvent.objects.all()
    if action:
        qs = qs.filter(action=action)
    if entity_type:
        qs = qs.filter(entity_type=entity_type)
    if entity_id:
        qs = qs.filter(entity_id=entity_id)
    if user:
        qs = qs.filter(user_id=user)
    if actor_kind:
        qs = qs.filter(actor_kind=actor_kind)
    if since:
        qs = qs.filter(at__date__gte=since)
    if until:
        qs = qs.filter(at__date__lte=until)
    if q:
        qs = qs.filter(summary__icontains=q)
    return qs.order_by("-at")


@router.get("/{event_id}", response=AuditEventOut)
def get_event(request, event_id: UUID):
    access.require(request.actor, "core.view_audit")
    return get_object_or_404(AuditEvent, pk=event_id)
