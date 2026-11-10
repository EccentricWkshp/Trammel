"""Change orders become quotes of their own kind (0.4.3).

Each change order becomes a change-order quote with the same id and number
and one revision; each of its lines becomes an item line with the same id, so
invoice lines already billed from it still point at it (billing 0008). An
approved one gets its approval record, and the operation it added to a work
order is marked as coming from it, so it is never added again.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

CENT = Decimal("0.01")
HOW = {"in_person", "phone", "email", "other"}


def _money(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def forward(apps, schema_editor):
    ChangeOrder = apps.get_model("workorders", "ChangeOrder")
    Operation = apps.get_model("workorders", "Operation")
    Quote = apps.get_model("quotes", "Quote")
    QuoteRevision = apps.get_model("quotes", "QuoteRevision")
    QuoteLine = apps.get_model("quotes", "QuoteLine")
    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    Approval = apps.get_model("quotes", "Approval")
    CustomerAgreement = apps.get_model("agreements", "CustomerAgreement")

    template = QuoteTemplate.objects.filter(is_active=True).order_by("-is_default", "name").first()
    for co in ChangeOrder.objects.order_by("created_at"):
        quote = Quote.objects.create(
            id=co.id,
            job_id=co.job_id,
            kind="change_order",
            number=co.number,
            title=co.title,
            created_at=co.created_at,
            created_by_id=co.created_by_id,
            deleted_at=co.deleted_at,
        )
        lines = list(co.lines.order_by("description", "id"))
        cost_total = sum((_money(line.quantity * line.unit_cost) for line in lines), Decimal("0"))
        revision = QuoteRevision.objects.create(
            quote=quote,
            number=1,
            status=co.status,
            template=template,
            description=co.description,
            amended_terms=co.amended_terms,
            lines_total=co.total,
            subtotal=co.total,
            total=co.total,
            cost_total=cost_total,
            sent_at=co.updated_at if co.status != "draft" else None,
            created_at=co.created_at,
            created_by_id=co.created_by_id,
            deleted_at=co.deleted_at,
        )
        for position, line in enumerate(lines, start=1):
            QuoteLine.objects.create(
                id=line.id,
                revision=revision,
                position=position * 10,
                kind="adhoc",
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                unit_cost=line.unit_cost,
                line_total=line.line_total,
                cost_total=_money(line.quantity * line.unit_cost),
                tax_category_id=line.tax_category_id,
                split={line.category: str(line.line_total)},
            )
        if co.status != "approved":
            continue
        agreement = (
            CustomerAgreement.objects.filter(pk=co.agreement_id).first()
            if co.agreement_id
            else None
        )
        Approval.objects.create(
            revision=revision,
            method="manual",
            how=co.approval_how if co.approval_how in HOW else "",
            approved_by_name=co.approved_by_name or "Not recorded",
            approved_at=co.approved_at or co.updated_at,
            notes="Approved before change orders had quote-level detail (0.4.3).",
            snapshot={
                "quote": co.number,
                "revision": 1,
                "totals": {"total": str(co.total)},
                "lines": [
                    {
                        "description": line.description,
                        "quantity": str(line.quantity),
                        "unit": line.unit,
                        "unit_price": str(line.unit_price),
                        "line_total": str(line.line_total),
                    }
                    for line in lines
                ],
            },
            agreement=agreement,
            agreement_text=agreement.text if agreement else "",
        )
        added = (
            Operation.objects.filter(
                work_order__job_id=co.job_id,
                parent__isnull=True,
                name__startswith=f"Change order {co.number}:",
            )
            .order_by("id")
            .first()
        )
        if added is not None:
            added.source_revision = revision
            added.save(update_fields=["source_revision"])


class Migration(migrations.Migration):
    dependencies = [
        ("workorders", "0006_operation_source_revision"),
        ("quotes", "0010_revision_description"),
        ("agreements", "0003_help_text"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
