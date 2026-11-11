from datetime import timedelta

from trammel.core.tasks import periodic


@periodic("portal.prune", every=timedelta(hours=24))
def prune(payload):
    """Clear sign-in codes and rate-limit records older than a month."""
    from .services import prune as run

    return run()
