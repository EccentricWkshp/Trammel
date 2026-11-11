from datetime import timedelta

from trammel.core.tasks import periodic, task


@periodic("portal.prune", every=timedelta(hours=24))
def prune(payload):
    """Clear sign-in codes and rate-limit records older than a month."""
    from .services import prune as run

    return run()


@task("portal.request_code")
def request_code(payload):
    """Look up the address and send its sign-in code, away from the request."""
    from .services import request_code as run

    run(payload.get("email", ""), payload.get("ip"))
