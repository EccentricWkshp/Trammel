from datetime import timedelta

from trammel.core.tasks import periodic


@periodic("quotes.expire", every=timedelta(hours=1))
def expire_quotes(payload):
    """Sent quotes past their validity date become expired (FR-QUO-7)."""
    from .services import expire_due

    expire_due()
