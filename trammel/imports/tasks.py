from trammel.core.tasks import task


@task("imports.shopify_order")
def shopify_order(payload):
    """An order Shopify sent by webhook, already verified, brought in."""
    from . import services, shopify

    result = services.import_orders([shopify.read(payload.get("order") or {})], "shopify")
    if result.errors:
        raise RuntimeError("; ".join(result.errors))
