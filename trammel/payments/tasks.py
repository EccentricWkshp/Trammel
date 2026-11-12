from datetime import timedelta

from trammel.core.tasks import periodic, task


@periodic("payments.check", every=timedelta(seconds=60))
def check(payload):
    """Record what customers paid online, and keep each open invoice's link to its balance."""
    from . import services

    services.run_check()


@task("payments.keep_links")
def keep_links(payload):
    """An invoice's balance changed, or it was issued or voided: its link follows."""
    from . import services, stripe
    from .models import StripeSettings

    if not services.active():
        return
    try:
        services.keep_links([payload["invoice"]] if payload.get("invoice") else None)
    except stripe.StripeError as exc:
        from django.utils import timezone

        StripeSettings.objects.filter(pk=1).update(
            last_error=str(exc)[:2000], last_error_at=timezone.now()
        )
