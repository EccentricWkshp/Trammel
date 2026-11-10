from datetime import timedelta

from trammel.core.tasks import periodic


@periodic("purchasing.late_notice", every=timedelta(hours=24))
def late_delivery_notice(payload):
    """Tell staff about deliveries past their expected date. It informs only:
    chasing the vendor stays with people."""
    from trammel.core import modules

    if modules.is_enabled("purchasing") and modules.is_enabled("notifications"):
        from .services import notify_late_deliveries

        notify_late_deliveries()
