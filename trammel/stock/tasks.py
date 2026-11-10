from datetime import timedelta

from trammel.core.tasks import periodic


@periodic("stock.low_notice", every=timedelta(hours=1))
def low_stock_notice(payload):
    """Tell staff when materials reach their restock level. It informs only:
    deciding to buy and placing the order stay with people."""
    from trammel.core import modules

    if modules.is_enabled("stock") and modules.is_enabled("notifications"):
        from .services import notify_low_stock

        notify_low_stock()
