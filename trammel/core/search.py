"""Global search (FR-SRCH-1).

Each module registers a searcher; core runs them and never imports a module.
Identifiers (document numbers, SKUs, names, emails, phone numbers) are matched
as substrings — the end of a phone number must find the customer — and longer
text (scope, notes, descriptions) with PostgreSQL full-text search.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db.models import Q, QuerySet

_searchers: list[tuple[str, Callable]] = []


@dataclass(frozen=True)
class Hit:
    kind: str
    label: str
    url: str
    detail: str = ""


def register(kind: str):
    """Register `fn(actor, term) -> Iterable[Hit]` under a heading such as "Jobs"."""

    def decorator(fn):
        _searchers.append((kind, fn))
        return fn

    return decorator


def matching(
    qs: QuerySet, term: str, identifiers: tuple[str, ...], text: tuple[str, ...] = ()
) -> QuerySet:
    q = Q()
    for field in identifiers:
        q |= Q(**{f"{field}__icontains": term})
    if text:
        qs = qs.annotate(_search=SearchVector(*text, config="english"))
        q |= Q(_search=SearchQuery(term, config="english", search_type="websearch"))
    return qs.filter(q)


def run(actor, term: str, limit: int = 10) -> list[tuple[str, list[Hit]]]:
    term = term.strip()
    if len(term) < 2:
        return []
    results = []
    for kind, fn in _searchers:
        hits = list(fn(actor, term))[:limit]
        if hits:
            results.append((kind, hits))
    return results
