"""
Audit log recording (FR-AUD-1, FR-AUD-2).

The acting principal and source address are bound once per request by
`AuditContextMiddleware` (or by `acting_as()` in commands and jobs), so service
code calls `record()` without threading the request through every layer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

from django.db import models

from .access import SYSTEM, Actor, as_actor


@dataclass(frozen=True)
class AuditContext:
    actor: Actor
    ip: str | None = None
    source: str = "web"


_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def current() -> AuditContext:
    return _context.get() or AuditContext(actor=SYSTEM, source="system")


def current_actor() -> Actor:
    return current().actor


@contextmanager
def acting_as(actor, *, ip: str | None = None, source: str | None = None) -> Iterator[None]:
    """Bind the principal for a block — used by middleware, commands and jobs."""
    actor = as_actor(actor)
    token = _context.set(
        AuditContext(actor=actor, ip=ip, source=source or ("system" if actor.is_system else "web"))
    )
    try:
        yield
    finally:
        _context.reset(token)


def rebind(actor: Actor, *, source: str | None = None) -> None:
    """Replace the actor for the rest of the current context.

    For authenticators that identify the principal partway through a request
    (an API key). The enclosing `acting_as()` restores the outer value on exit.
    """
    ctx = current()
    _context.set(AuditContext(actor=actor, ip=ctx.ip, source=source or ctx.source))


# ---------------------------------------------------------------------------
# Snapshots and diffs
# ---------------------------------------------------------------------------


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, models.Model):
        return str(value.pk)
    if isinstance(value, bytes | bytearray | memoryview):
        # Binary content (a logo) is recorded by size and fingerprint, not inline.
        data = bytes(value)
        if not data:
            return ""
        return f"{len(data):,} bytes, sha256 {hashlib.sha256(data).hexdigest()[:12]}"
    if hasattr(value, "name") and hasattr(value, "url"):  # FieldFile
        return value.name or ""
    return value


#: Never written to the audit log, whatever the model.
_SECRET_FIELDS = frozenset(
    {"password", "secret_hash", "ciphertext", "totp_secret", "recovery_codes", "token_hash"}
)


def snapshot(obj: models.Model, fields: Iterable[str] | None = None) -> dict:
    """A JSON-safe dict of a model's concrete field values."""
    out = {}
    for f in obj._meta.concrete_fields:
        if f.name in _SECRET_FIELDS or (fields is not None and f.name not in fields):
            continue
        if f.name in {"created_at", "updated_at"}:
            continue
        out[f.name] = _jsonable(getattr(obj, f.attname))
    return out


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    """Reduce two snapshots to only the keys that changed."""
    keys = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def entity_type_of(obj: models.Model) -> str:
    return obj._meta.label_lower


def record(
    action: str,
    entity: models.Model | None = None,
    *,
    summary: str = "",
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
    entity_type: str = "",
    entity_id: str = "",
    entity_label: str = "",
    actor: Actor | None = None,
):
    """Append one audit event. Returns it."""
    from .models import AuditEvent

    ctx = current()
    actor = as_actor(actor) if actor is not None else ctx.actor
    if entity is not None:
        entity_type = entity_type or entity_type_of(entity)
        entity_id = entity_id or str(entity.pk)
        entity_label = entity_label or str(entity)[:200]
    return AuditEvent.objects.create(
        actor_kind=actor.kind,
        user=actor.user,
        customer_id=actor.customer_id,
        api_key_id=actor.api_key_id,
        actor_label=actor.label[:200],
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label[:200],
        summary=summary[:300],
        reason=reason,
        before=_jsonable(before or {}),
        after=_jsonable(after or {}),
        ip=ctx.ip,
        source="api" if actor.api_key_id else ctx.source,
    )


def record_create(obj: models.Model, *, summary: str = "") -> None:
    record(
        "create",
        obj,
        summary=summary or f"Created {obj._meta.verbose_name} {obj}",
        after=snapshot(obj),
    )


def record_update(obj: models.Model, before: dict, *, summary: str = "", reason: str = "") -> None:
    """Record an edit from a snapshot taken before it. Silent when nothing changed."""
    old, new = diff(before, snapshot(obj))
    if not old and not new:
        return
    record(
        "update",
        obj,
        summary=summary or f"Edited {obj._meta.verbose_name} {obj}",
        before=old,
        after=new,
        reason=reason,
    )


def record_delete(obj: models.Model, *, reason: str = "") -> None:
    record("delete", obj, summary=f"Deleted {obj._meta.verbose_name} {obj}", reason=reason)


def record_restore(obj: models.Model) -> None:
    record("restore", obj, summary=f"Restored {obj._meta.verbose_name} {obj}")
