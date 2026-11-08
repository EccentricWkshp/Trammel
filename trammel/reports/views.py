from __future__ import annotations

import datetime as dt

from django.shortcuts import render
from django.utils import timezone

from trammel.core import audit
from trammel.core.web import csv_response, permission_required

from . import services


def _period(request) -> tuple[dt.date, dt.date]:
    today = timezone.localdate()
    try:
        start = dt.date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today.replace(day=1)
    try:
        end = dt.date.fromisoformat(request.GET.get("end", ""))
    except ValueError:
        end = today
    return (start, end) if start <= end else (end, start)


def _csv(request, name, header, rows):
    audit.record("export", entity_type="reports", summary=f"Exported the {name} report as CSV")
    return csv_response(f"{name}.csv", header, rows)


def _wants_csv(request) -> bool:
    return request.GET.get("format") == "csv"


@permission_required("reports.view")
def index(request):
    return render(request, "reports/index.html")


@permission_required("core.see_costs")
def profitability(request):
    start, end = _period(request)
    rows, totals = services.profitability(start, end)
    if _wants_csv(request):
        return _csv(
            request,
            "job-profitability",
            [
                "job",
                "customer",
                "quoted",
                "quoted_cost",
                "actual_cost",
                "invoiced",
                "margin",
                "margin_pct",
            ],
            (
                (
                    r["job"].number,
                    r["job"].customer.name,
                    r["quoted"],
                    r["quoted_cost"],
                    r["actual"],
                    r["invoiced"],
                    r["margin"],
                    r["margin_pct"],
                )
                for r in rows
            ),
        )
    return render(
        request,
        "reports/profitability.html",
        {"rows": rows, "totals": totals, "start": start, "end": end},
    )


@permission_required("reports.view")
def sales(request):
    start, end = _period(request)
    group = request.GET.get("group", "customer")
    if group not in ("customer", "product", "month"):
        group = "customer"
    rows, total = services.sales(start, end, group)
    if _wants_csv(request):
        return _csv(request, f"sales-by-{group}", [group, "amount"], rows)
    return render(
        request,
        "reports/sales.html",
        {"rows": rows, "total": total, "start": start, "end": end, "group": group},
    )


@permission_required("reports.view")
def aging(request):
    try:
        as_of = dt.date.fromisoformat(request.GET.get("as_of", ""))
    except ValueError:
        as_of = timezone.localdate()
    rows, totals = services.ar_aging(as_of)
    if _wants_csv(request):
        keys = [b for b, _ in services.BUCKETS]
        return _csv(
            request,
            "ar-aging",
            ["customer", *keys, "total"],
            ([r["customer"].name, *[r[k] for k in keys], r["total"]] for r in rows),
        )
    return render(
        request,
        "reports/aging.html",
        {"rows": rows, "totals": totals, "as_of": as_of, "buckets": services.BUCKETS},
    )


@permission_required("reports.view")
def tax(request):
    start, end = _period(request)
    data = services.tax_report(start, end)
    if _wants_csv(request):
        rows = [
            (
                r["code"],
                r["invoices"],
                r["gross"],
                r["taxable"],
                r["exempt"],
                r["tax"],
                r["flagged"],
            )
            for r in data["rows"]
        ]
        rows += [
            ("Gross receipts: " + b["classification"], "", b["basis"], "", "", b["tax"] or "", "")
            for b in data["bo"]
        ]
        rows.append(("Bad debt deduction", "", data["bad_debt"], "", "", "", ""))
        return _csv(
            request,
            "tax",
            ["jurisdiction", "invoices", "gross", "taxable", "exempt", "tax", "flagged"],
            rows,
        )
    return render(request, "reports/tax.html", {**data, "start": start, "end": end})


@permission_required("reports.view")
def time(request):
    start, end = _period(request)
    group = request.GET.get("group", "user")
    if group not in ("user", "machine", "job"):
        group = "user"
    rows, available = services.time_report(start, end, group)
    if _wants_csv(request):
        return _csv(
            request,
            f"time-by-{group}",
            [group, "hours", "cost", "utilization_pct"],
            ((r["name"], r["hours"], r["cost"], r["utilization"] or "") for r in rows),
        )
    return render(
        request,
        "reports/time.html",
        {
            "rows": rows,
            "start": start,
            "end": end,
            "group": group,
            "available": available,
            "workday": services.WORKDAY_HOURS,
        },
    )


@permission_required("reports.view")
def force_closed(request):
    rows, write_offs = services.force_closed()
    if _wants_csv(request):
        return _csv(
            request,
            "force-closed",
            ["when", "job", "by", "reason", "outstanding"],
            (
                (
                    r["change"].at.isoformat(),
                    r["job"].number if r["job"] else "",
                    str(r["change"].user or ""),
                    r["change"].reason,
                    "; ".join(f"{o['label']}: {o['resolution']}" for o in r["outstanding"]),
                )
                for r in rows
            ),
        )
    return render(request, "reports/force_closed.html", {"rows": rows, "write_offs": write_offs})
