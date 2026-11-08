"""Starting data for the agreements module (see ModuleManifest.seed)."""

from __future__ import annotations

DEFAULT_BODY = """\
TERMS AND CONDITIONS

These terms apply to job {{ job.number }} ("{{ job.title }}") between {{ business.legal_name }}
("the shop") and {{ customer.name }}{% if customer.company %} of {{ customer.company }}{% endif %}
("the customer").

1. Scope. The shop will perform the work described in the approved quote. Changes to the
   scope are made by a written change order, approved before the changed work begins.

2. Deposit and payment.{% if deposit_percent %} A deposit of {{ deposit_percent }}% is due on approval.{% endif %}
   Invoices are due within {{ payment_terms_days }} days of the invoice date.

3. Materials and approvals. Natural materials vary in color and grain. Designs, drawings and
   proofs approved by the customer are produced as approved.

4. Completion and pickup. Dates are estimates unless agreed in writing. Finished work not
   collected within 30 days of notice of completion may incur storage charges.

5. Cancellation. If the customer cancels after work has started, the customer pays for work
   performed and materials purchased for the job.

6. Warranty. The shop warrants its workmanship for 90 days from delivery. The warranty does
   not cover misuse, modification, or normal wear.

Issued {{ today|date }}.
"""


def seed() -> None:
    """Create a default template if there are none at all. Not legal advice; edit it."""
    from .models import AgreementTemplate, AgreementTemplateVersion

    if AgreementTemplate.all_objects.exists():
        return
    template = AgreementTemplate.objects.create(name="Standard terms", is_default=True)
    AgreementTemplateVersion.objects.create(template=template, version=1, body=DEFAULT_BODY)
