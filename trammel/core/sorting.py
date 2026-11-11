"""Sorting list pages by their columns.

A page names the ways it can be sorted, each with its label and the fields to
order by, and its default:

    SORTS = sorting.Sorts(
        {
            "number": sorting.By("Number", "number"),
            "due": sorting.By("Due date", "due_date"),
            "created": sorting.By("Newest", "created_at", newest_first=True),
        },
        default="due",
    )
    qs, ctx = SORTS.apply(request, qs)

`?sort=key` sorts by it, `?sort=-key` the other way round. `hide` leaves out
sorts by columns the person can't see (a cost, without permission to see
costs), so the order can't give away what the column would show. Blank values sort
last either way, and ties fall back to the record's id so pages don't shuffle.
In the template, `{% sort_th "due" "Due" %}` is a column heading that sorts by
its column (a second click reverses), and `{% sort_select %}` the same choices
as a menu, for the page's filter form.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Case, F, IntegerField, Value, When
from django.db.models.expressions import BaseExpression


@dataclass(frozen=True)
class By:
    label: str
    fields: tuple
    #: Shown newest or highest first when first picked (dates of record,
    #: amounts), rather than A to Z.
    newest_first: bool = False

    def __init__(self, label: str, *fields, newest_first: bool = False):
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "fields", tuple(fields))
        object.__setattr__(self, "newest_first", newest_first)


def in_order(field: str, values) -> Case:
    """Order by a choice field in the order its choices are listed (a status in
    lifecycle order, not alphabetically)."""
    return Case(
        *(When(**{field: v}, then=Value(i)) for i, v in enumerate(values)),
        default=Value(len(values)),
        output_field=IntegerField(),
    )


def _ordering(field, descending: bool):
    """One ordering term, blanks last either way. A field written "-name" is
    already reversed (highest priority first, say)."""
    if isinstance(field, str):
        if field.startswith("-"):
            field, descending = field[1:], not descending
        field = F(field)
    if not isinstance(field, BaseExpression | F):
        raise TypeError(f"Can't sort by {field!r}.")
    return field.desc(nulls_last=True) if descending else field.asc(nulls_last=True)


class Sorts:
    def __init__(self, choices: dict[str, By], *, default: str):
        self.choices = choices
        self.default = default

    def current(self, request, hide=()) -> tuple[str, bool]:
        """(key, reversed) from ?sort=, else the default."""
        raw = request.GET.get("sort", "") or self.default
        reverse = raw.startswith("-")
        key = raw.lstrip("-")
        if key not in self.choices or key in hide:
            raw = self.default
            reverse, key = raw.startswith("-"), raw.lstrip("-")
        return key, reverse

    def apply(self, request, qs, *, hide=()):
        """The queryset sorted, and the template context for the headings."""
        key, reverse = self.current(request, hide)
        by = self.choices[key]
        descending = by.newest_first != reverse
        terms = [_ordering(f, descending) for f in by.fields]
        qs = qs.order_by(*terms, "-pk" if descending else "pk")
        return qs, {"sorting": self.context(request, key, reverse, hide)}

    def context(self, request, key: str, reverse: bool, hide=()) -> dict:
        params = request.GET.copy()
        params.pop("page", None)

        def url(value: str) -> str:
            q = params.copy()
            q["sort"] = value
            return "?" + q.urlencode()

        shown = {k: by for k, by in self.choices.items() if k not in hide}
        links = {}
        for k, by in shown.items():
            active = k == key
            # A second click on the sorted column reverses it.
            target = f"-{k}" if active and not reverse else k
            descending = by.newest_first != (reverse if active else False)
            links[k] = {
                "url": url(target),
                "active": active,
                "aria": ("descending" if descending else "ascending") if active else "",
                "arrow": ("▼" if descending else "▲") if active else "",
            }
        options = [(k, by.label) for k, by in shown.items()]
        if reverse:
            options.insert(
                [k for k, _ in options].index(key) + 1,
                (f"-{key}", f"{self.choices[key].label}, reversed"),
            )
        return {
            "key": key,
            "value": f"-{key}" if reverse else key,
            "links": links,
            "options": options,
        }
