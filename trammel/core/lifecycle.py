"""
Lifecycle enforcement (SPEC §3.3, §6).

A model's status changes only through `Lifecycle.apply()`. Each allowed move is
declared as a `Transition`:

* **forward** — the normal path; guards (e.g. closeout preconditions) must pass.
* **reopen** — one step back, with a reason and the reopen permission (§6.2).
* **force** — skips guards, with a reason and its own permission; the caller
  supplies the outstanding state to store (§6.2 forced closeout).

`LifecycleModel` refuses a save that changes the status field any other way, so
there is no path — form, API, or shell — that can skip a state by assignment.
Each applied transition writes a `StateChange` row and an audit event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction

from . import access, audit
from .models import StateChange

Guard = Callable[[models.Model], Iterable[str]]
Hook = Callable[[models.Model, "Transition", access.Actor], None]


class TransitionError(ValidationError):
    """A refused transition. `problems` lists every reason, for the screen."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(problems)


@dataclass(frozen=True)
class Transition:
    source: str | tuple[str, ...]
    target: str
    kind: str = StateChange.Kind.FORWARD
    #: Permission codename required, beyond the caller's own checks.
    permission: str | None = None
    requires_reason: bool = False
    guards: tuple[Guard, ...] = ()
    #: Runs inside the transaction after the status is saved — for cascades
    #: such as a work order reopen moving its job back to in_progress.
    after: tuple[Hook, ...] = ()
    label: str = ""
    #: Whether the transition exists at all for this record. Unlike a guard,
    #: a transition that does not apply is not offered — e.g. "resume" from
    #: on_hold applies only towards the state the record was held from.
    when: Callable[[models.Model], bool] | None = None

    def __post_init__(self):
        if self.kind != StateChange.Kind.FORWARD and not self.requires_reason:
            object.__setattr__(self, "requires_reason", True)

    def sources(self) -> tuple[str, ...]:
        return (self.source,) if isinstance(self.source, str) else self.source

    def applies(self, obj: models.Model, state: str) -> bool:
        return state in self.sources() and (self.when is None or self.when(obj))


@dataclass
class Lifecycle:
    field: str
    initial: str
    transitions: list[Transition] = field(default_factory=list)

    def available(self, obj: models.Model, actor=None) -> list[Transition]:
        """Transitions out of the current state the actor holds permission for."""
        state = getattr(obj, self.field)
        out = []
        for t in self.transitions:
            if not t.applies(obj, state):
                continue
            if actor is not None and t.permission and not access.has_perm(actor, t.permission, obj):
                continue
            out.append(t)
        return out

    def find(self, obj: models.Model, target: str, kind: str | None = None) -> Transition | None:
        state = getattr(obj, self.field)
        for t in self.transitions:
            if t.applies(obj, state) and t.target == target and (kind is None or t.kind == kind):
                return t
        return None

    def problems(self, obj: models.Model, transition: Transition) -> list[str]:
        if transition.kind == StateChange.Kind.FORCE:
            return []
        return [p for guard in transition.guards for p in guard(obj)]

    def apply(
        self,
        obj: models.Model,
        target: str,
        *,
        actor=None,
        reason: str = "",
        kind: str | None = None,
        details: dict | None = None,
    ) -> StateChange:
        actor = access.as_actor(actor) if actor is not None else audit.current_actor()
        with transaction.atomic():
            # Lock and re-read so two people pressing the button cannot both
            # move the record from the same starting state.
            locked = type(obj)._base_manager.select_for_update().get(pk=obj.pk)
            current = getattr(locked, self.field)
            transition = self.find(locked, target, kind)
            if transition is None:
                raise TransitionError([f"Cannot move from “{current}” to “{target}”."])
            if transition.permission and not access.has_perm(actor, transition.permission, obj):
                raise PermissionDenied(f"Requires the {transition.permission} permission.")
            if transition.requires_reason and not reason.strip():
                raise TransitionError(["A reason is required."])
            problems = self.problems(locked, transition)
            if problems:
                raise TransitionError(problems)

            setattr(obj, self.field, target)
            obj._lifecycle_permit = True
            try:
                obj.save(update_fields=[self.field, *_touch_fields(obj)])
            finally:
                obj._lifecycle_permit = False
            obj._loaded_state = target

            change = StateChange.objects.create(
                entity_type=audit.entity_type_of(obj),
                entity_id=obj.pk,
                field=self.field,
                from_state=current,
                to_state=target,
                kind=transition.kind,
                actor_kind=actor.kind,
                user=actor.user,
                reason=reason.strip(),
                details=details or {},
            )
            action = {"forward": "transition", "reopen": "reopen", "force": "force"}[
                transition.kind
            ]
            audit.record(
                action,
                obj,
                summary=f"{obj._meta.verbose_name.capitalize()} {obj}: {current} → {target}",
                before={self.field: current},
                after={self.field: target, **({"details": details} if details else {})},
                reason=reason.strip(),
                actor=actor,
            )
            for hook in transition.after:
                hook(obj, transition, actor)
        return change


def _touch_fields(obj: models.Model) -> list[str]:
    return ["updated_at"] if any(f.name == "updated_at" for f in obj._meta.concrete_fields) else []


class LifecycleModel(models.Model):
    """Mixin that makes `lifecycle.field` writable only through the lifecycle."""

    lifecycle: Lifecycle

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db, field_names, values, *args, **kwargs):
        instance = super().from_db(db, field_names, values, *args, **kwargs)
        instance._loaded_state = getattr(instance, cls.lifecycle.field, None)
        return instance

    def save(self, *args, **kwargs):  # noqa: DJ012 — from_db is a Django hook too
        name = self.lifecycle.field
        state = getattr(self, name)
        if self._state.adding:
            if state != self.lifecycle.initial:
                raise TransitionError(
                    [f"A new {self._meta.verbose_name} must start as “{self.lifecycle.initial}”."]
                )
        elif not getattr(self, "_lifecycle_permit", False):
            if state != getattr(self, "_loaded_state", state):
                raise TransitionError([f"Change {name} through its lifecycle, not by assignment."])
            # An ordinary edit never writes the status column, so saving an
            # instance loaded before someone else's transition cannot undo it.
            fields = kwargs.get("update_fields")
            if fields is None:
                fields = [f.name for f in self._meta.concrete_fields if not f.primary_key]
            kwargs["update_fields"] = [f for f in fields if f != name]
        super().save(*args, **kwargs)
        self._loaded_state = state

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._loaded_state = getattr(self, self.lifecycle.field)

    def history(self):
        return StateChange.objects.filter(entity_type=audit.entity_type_of(self), entity_id=self.pk)
