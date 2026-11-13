"""
The permission layer (SPEC §2).

Every check answers two questions, and the second is built in from the start
rather than retrofitted:

1. May this principal perform this action at all?  (`has_perm`)
2. May it perform it on *this record*?               (`scoped`, `get_or_404`)

Principals are users, customers (portal), API keys and the system. Each is
represented by an `Actor`. A user's permissions are the union of their roles'
permission codenames; the system admin holds every permission. An API key holds
the intersection of its scope and its user's permissions (FR-API-2). A customer
holds only permissions declared `portal=True`, and only on records that lead
back to it through the model's `customer_scope_path`.

A permission owned by a disabled module is refused unless it is a read
permission, which is how "disabling keeps data, read-only" is enforced
(SPEC §4.5) without every view remembering to check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Model, Q, QuerySet
from django.http import Http404

from . import modules

if TYPE_CHECKING:
    from trammel.accounts.models import User


class UnknownPermission(LookupError):
    """A codename no installed module declares — always a programming error."""


@dataclass(frozen=True, eq=False)
class Actor:
    kind: str  # one of core.models.ActorKind
    user: User | None = None
    customer_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    #: An API key's scope; None means "everything the user holds".
    scope: frozenset[str] | None = None
    label: str = ""
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def is_system(self) -> bool:
        return self.kind == "system"

    @property
    def is_customer(self) -> bool:
        return self.kind == "customer"

    @property
    def is_authenticated(self) -> bool:
        return self.kind != "anonymous"


SYSTEM = Actor(kind="system", label="System")
ANONYMOUS = Actor(kind="anonymous", label="Anonymous")


def user_actor(user: User) -> Actor:
    return Actor(kind="user", user=user, label=user.display_name)


def as_actor(principal: Any) -> Actor:
    """Accept an Actor, a User, an AnonymousUser or None."""
    if isinstance(principal, Actor):
        return principal
    if principal is None or not getattr(principal, "is_authenticated", False):
        return ANONYMOUS
    return user_actor(principal)


def is_admin(actor: Any) -> bool:
    """The built-in system admin role (SPEC §2)."""
    actor = as_actor(actor)
    return (
        actor.user is not None
        and actor.user.is_active
        and actor.user.is_admin
        and (actor.scope is None)
    )


def require_admin(actor: Any) -> None:
    if not is_admin(actor):
        raise PermissionDenied


# ---------------------------------------------------------------------------
# Action-level checks
# ---------------------------------------------------------------------------


def _registered(codename: str) -> modules.Permission:
    try:
        return modules.all_permissions()[codename]
    except KeyError:
        raise UnknownPermission(codename) from None


def permissions_of(actor: Actor) -> frozenset[str]:
    """Every codename the actor holds, before module and record checks."""
    if "perms" in actor._cache:
        return actor._cache["perms"]
    everything = frozenset(modules.all_permissions())
    if actor.is_system:
        held = everything
    elif actor.is_customer:
        held = frozenset(c for c, p in modules.all_permissions().items() if p.portal)
    elif actor.user is None or not actor.user.is_active:
        held = frozenset()
    elif actor.user.is_admin:
        held = everything
    else:
        held = frozenset(
            codename
            for role in actor.user.roles.all()
            for codename in role.permissions
            if codename in everything
        )
    if not actor.is_customer:
        held = held - frozenset(c for c, p in modules.all_permissions().items() if p.customer_only)
    if actor.scope is not None:
        held = held & actor.scope
    actor._cache["perms"] = held
    return held


def has_perm(actor: Any, codename: str, obj: Model | None = None) -> bool:
    actor = as_actor(actor)
    perm = _registered(codename)
    if not perm.read and not modules.is_enabled(perm.module):
        return False
    if codename not in permissions_of(actor):
        return False
    if obj is not None:
        return record_allowed(actor, obj)
    return True


def sees_costs(context_or_actor: Any) -> bool:
    """Whether costs and margins may be shown: the actor holds core.see_costs.
    Takes an actor, or an API schema's resolver context (the request's actor,
    an API key's scopes included), so every API output applies the same rule
    as the pages (SEC-03, 0.10.5)."""
    actor = context_or_actor
    if isinstance(context_or_actor, dict):
        request = context_or_actor.get("request")
        actor = getattr(request, "actor", None) if request is not None else None
    if actor is None:
        return False
    return has_perm(actor, "core.see_costs")


def require(actor: Any, codename: str, obj: Model | None = None) -> None:
    """Raise unless allowed. A record the actor may not see at all is a 404."""
    actor = as_actor(actor)
    if obj is not None and not record_allowed(actor, obj):
        raise Http404
    if not has_perm(actor, codename):
        raise Http404 if actor.is_customer else PermissionDenied


# ---------------------------------------------------------------------------
# Record-level checks
# ---------------------------------------------------------------------------


def record_filter(actor: Actor, model: type[Model]) -> Q | None:
    """A filter confining `actor` to the rows of `model` it may touch.

    None means unrestricted. Staff users are unrestricted today; this is the
    single place an "only my jobs" role rule would be added.
    """
    if actor.is_system:
        return None
    if actor.is_customer:
        path = getattr(model, "customer_scope_path", None)
        if path is None or actor.customer_id is None:
            return Q(pk__in=[])
        return Q(**{path: actor.customer_id})
    if not actor.is_authenticated:
        return Q(pk__in=[])
    return None


def record_allowed(actor: Actor, obj: Model) -> bool:
    q = record_filter(actor, type(obj))
    if q is None:
        return True
    return type(obj)._base_manager.filter(pk=obj.pk).filter(q).exists()


def scoped(actor: Any, codename: str, queryset: QuerySet) -> QuerySet:
    """`queryset` narrowed to what `actor` may do `codename` to. Empty if nothing."""
    actor = as_actor(actor)
    if not has_perm(actor, codename):
        return queryset.none()
    q = record_filter(actor, queryset.model)
    return queryset if q is None else queryset.filter(q)


def get_or_404(actor: Any, codename: str, queryset: QuerySet, **lookup) -> Model:
    """Fetch one record the actor may act on.

    Lacking the permission altogether is 403 for staff (they know the screen
    exists). A record outside the actor's scope is 404, never 403, so a
    customer cannot probe for other customers' record ids.
    """
    actor = as_actor(actor)
    if not has_perm(actor, codename):
        raise Http404 if actor.is_customer else PermissionDenied
    q = record_filter(actor, queryset.model)
    if q is not None:
        queryset = queryset.filter(q)
    try:
        return queryset.get(**lookup)
    except queryset.model.DoesNotExist, ValidationError, ValueError, TypeError:
        raise Http404 from None
