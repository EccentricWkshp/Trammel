"""Sections of the home screen, contributed by the modules that own the data.

    @dashboard.register("jobs.priority", "Priority jobs", order=10, permission="jobs.view")
    def priority_jobs(request): return dashboard.Section(rows=[...], template="...")

A section function returns a `Section`, or None to stay hidden (nothing to show
is still a Section, with no rows, so the user sees it is clear). Sections of a
disabled module, or that the user lacks the permission for, are left out.

Each user chooses which sections they see and in what order (Customize home);
the choice is stored on the user as [{"key": ..., "on": bool}, ...]. Sections
added later, by an upgrade or a newly enabled module, appear in their default
place until the user says otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

#: Most rows a section shows; it links to the full list for the rest.
LIMIT = 10


@dataclass
class Section:
    template: str
    rows: list = field(default_factory=list)
    #: How many there are in all, when that is more than the rows shown.
    total: int | None = None
    more_url: str = ""
    empty: str = "Nothing here."
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SectionDef:
    key: str
    title: str
    fn: Callable
    order: int
    permission: str | None
    default_on: bool
    description: str


_sections: dict[str, SectionDef] = {}


def register(
    key: str,
    title: str,
    *,
    order: int = 100,
    permission: str | None = None,
    default_on: bool = True,
    description: str = "",
):
    def decorator(fn: Callable):
        _sections[key] = SectionDef(key, title, fn, order, permission, default_on, description)
        return fn

    return decorator


def available(request) -> list[SectionDef]:
    """Sections this user may have, in their default order."""
    from . import access, modules

    enabled = modules.enabled_keys()
    out = []
    for d in _sections.values():
        module = modules.module_of(d.fn.__module__)
        if module is not None and module not in enabled:
            continue
        if d.permission and not access.has_perm(request.actor, d.permission):
            continue
        out.append(d)
    return sorted(out, key=lambda d: (d.order, d.key))


def arranged(request) -> list[tuple[SectionDef, bool]]:
    """Every available section with whether it is on, in the user's order."""
    defs = {d.key: d for d in available(request)}
    prefs = [p for p in (request.user.home_sections or []) if p.get("key") in defs]
    chosen = [(defs[p["key"]], bool(p.get("on"))) for p in prefs]
    seen = {d.key for d, _ in chosen}
    # A section the user has not arranged yet (new in an upgrade, or from a module
    # just enabled) goes after the last one that comes before it by default.
    for d in defs.values():
        if d.key not in seen:
            at = max(
                (i + 1 for i, (other, _) in enumerate(chosen) if other.order < d.order), default=0
            )
            chosen.insert(at, (d, d.default_on))
    return chosen


def for_user(request) -> list[tuple[SectionDef, Section]]:
    out = []
    for d, on in arranged(request):
        if on and (section := d.fn(request)) is not None:
            out.append((d, section))
    return out


def save_prefs(user, keys: list[str], on: set[str]) -> None:
    from . import audit

    before = list(user.home_sections or [])
    user.home_sections = [{"key": k, "on": k in on} for k in keys]
    user.save(update_fields=["home_sections"])
    audit.record(
        "user.home_sections",
        user,
        summary=f"{user} arranged their home screen",
        before={"home_sections": before},
        after={"home_sections": user.home_sections},
    )
