"""Where an order came from, for services that gather other stores' orders
(ShipStation, Shippo).

Their orders name the store or marketplace they came from. An order from a
store Trammel imports itself isn't brought in twice: it only adds what the
store's own order is missing (services.import_orders). A marketplace that
collects and pays the sales tax itself is recorded as doing so.
"""

from __future__ import annotations

#: How a service names a store, to the importer's own key for it.
STORES = {
    "shopify": "shopify",
    "etsy": "etsy",
    "ebay": "ebay",
    "woocommerce": "woocommerce",
    "woo commerce": "woocommerce",
    "woo": "woocommerce",
}
#: Marketplaces that collect and pay the sales tax on their sales (marketplace
#: facilitator laws): the label the sale records it under.
FACILITATORS = {
    "etsy": "Etsy",
    "ebay": "eBay",
    "amazon": "Amazon",
    "walmart": "Walmart",
}


def key(name: str | None) -> str:
    """A service's name for where an order came from, made comparable:
    "Amazon Seller Central" and "amazon" read the same."""
    text = (name or "").strip().lower().replace("_", " ")
    for known in (*STORES, *FACILITATORS):
        if text == known or text.startswith(f"{known} ") or text.startswith(known):
            return STORES.get(known, known)
    return text


def store(name: str | None) -> str:
    """The importer's key for the store an order came from, or "" when Trammel
    doesn't import that store itself."""
    found = key(name)
    return found if found in set(STORES.values()) else ""


def remitted_by(name: str | None) -> str:
    """The marketplace that pays the order's sales tax itself, or ""."""
    return FACILITATORS.get(key(name), "")


def number(value) -> str:
    """An order number as stores and services both write it: "#1001" and
    "1001" are the same order."""
    return str(value or "").strip().lstrip("#").strip().lower()
