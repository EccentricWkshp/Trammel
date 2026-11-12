from datetime import timedelta

from trammel.core.tasks import periodic, task


@periodic("payments.check", every=timedelta(seconds=60))
def check(payload):
    """Record what customers paid online, and keep each open invoice's link to its balance."""
    from . import services

    services.run_check()


@task("payments.keep_links")
def keep_links(payload):
    """An invoice's balance changed, it was issued or voided, or the way it's
    offered changed (its own choice, its customer's): its page follows."""
    from . import services
    from .errors import ProviderError

    if not services.active():
        return
    ids = payload.get("invoices") or ([payload["invoice"]] if payload.get("invoice") else None)
    try:
        services.keep_links(ids)
    except ProviderError as exc:
        services.note_problem(exc)
