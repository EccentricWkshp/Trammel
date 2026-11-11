from trammel.core.tasks import task


@task("accounts.magic_link")
def magic_link(payload):
    """Look up the address and send its sign-in link, away from the request."""
    from .services import send_magic_link

    send_magic_link(payload.get("email", ""), payload.get("ip"))
