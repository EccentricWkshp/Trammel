"""Online orders (FR-IMP): store connections, and every order brought in."""

from __future__ import annotations

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from trammel.core.models import SingletonModel, uuid7
from trammel.core.money import MoneyField


class ShopifySettings(SingletonModel):
    """One Shopify store. Its secrets are kept encrypted (core.secrets), never here."""

    class Auth(models.TextChoices):
        TOKEN = "token", "Admin API access token (a custom app made in the store's admin)"
        CLIENT = "client_credentials", "Client ID and secret (an app made in the Dev Dashboard)"

    enabled = models.BooleanField("connect Shopify", default=False)
    shop_domain = models.CharField(
        "store address",
        max_length=120,
        blank=True,
        help_text="The store's own your-store.myshopify.com address, not the domain your "
        "customers use.",
    )
    auth_mode = models.CharField(
        "how Trammel signs in",
        max_length=20,
        choices=Auth.choices,
        default=Auth.TOKEN,
    )
    client_id = models.CharField(
        "client ID", max_length=120, blank=True, help_text="From the app in the Dev Dashboard."
    )
    #: When the 24-hour token from the client ID and secret runs out (the token
    #: itself is an encrypted secret).
    access_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    webhooks_enabled = models.BooleanField(
        "accept order webhooks from Shopify",
        default=False,
        help_text="Only if Shopify can reach this Trammel from the internet. Orders then arrive "
        "within seconds; fetching by date still works either way and remains the way to "
        "catch up.",
    )

    class Meta:
        verbose_name = "Shopify settings"

    def __str__(self) -> str:
        return "Shopify settings"


class OAuthStore(SingletonModel):
    """A store the seller connects by approving Trammel on the store's own site
    (FR-IMP-5). The tokens are encrypted secrets, never here."""

    connected_at = models.DateTimeField(null=True, blank=True, editable=False)
    #: When the short-lived access token ends, and when the refresh token that
    #: renews it does.
    access_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        abstract = True

    @property
    def is_connected(self) -> bool:
        return bool(self.connected_at) and not self.has_expired

    @property
    def has_expired(self) -> bool:
        return bool(
            self.refresh_token_expires_at and self.refresh_token_expires_at <= timezone.now()
        )


class EtsySettings(OAuthStore):
    """One Etsy shop, connected by the seller approving Trammel on Etsy (OAuth with
    PKCE). Etsy keeps a connection for 90 days after it was last renewed. The
    shared secret is an encrypted secret."""

    enabled = models.BooleanField("connect Etsy", default=False)
    keystring = models.CharField(
        "keystring",
        max_length=120,
        blank=True,
        help_text="Your app's keystring, from Your apps on the Etsy developer site.",
    )
    #: Found from the account that approved Trammel.
    shop_id = models.CharField(max_length=40, blank=True, editable=False)
    shop_name = models.CharField(max_length=120, blank=True, editable=False)

    class Meta:
        verbose_name = "Etsy settings"

    def __str__(self) -> str:
        return "Etsy settings"


class EbaySettings(OAuthStore):
    """One eBay seller account, connected by the seller approving Trammel on eBay
    (authorization code with the app's RuName). The access token lasts two
    hours, the refresh token about 18 months. The Cert ID is an encrypted secret."""

    enabled = models.BooleanField("connect eBay", default=False)
    client_id = models.CharField(
        "App ID (Client ID)",
        max_length=120,
        blank=True,
        help_text="From the production keyset under Application Keys on the eBay developer site.",
    )
    ru_name = models.CharField(
        "RuName (eBay redirect URL name)",
        max_length=200,
        blank=True,
        help_text="The name eBay gives the redirect it sends sellers back by, under User "
        "Tokens, Get a Token from eBay via Your Application.",
    )
    sandbox = models.BooleanField(
        "use the eBay sandbox",
        default=False,
        help_text="eBay's test site, with its own keys. Leave off for your real orders.",
    )

    class Meta:
        verbose_name = "eBay settings"

    def __str__(self) -> str:
        return "eBay settings"


class WooCommerceSettings(SingletonModel):
    """One WooCommerce store, reached with a REST API key. The consumer key and
    secret are encrypted secrets, never here."""

    enabled = models.BooleanField("connect WooCommerce", default=False)
    store_url = models.CharField(
        "store address",
        max_length=200,
        blank=True,
        help_text="The WordPress site the store runs on, such as https://shop.example.com.",
    )

    class Meta:
        verbose_name = "WooCommerce settings"

    def __str__(self) -> str:
        return "WooCommerce settings"


class ShipStationSettings(SingletonModel):
    """A ShipStation account. Its API key and secret are encrypted secrets."""

    enabled = models.BooleanField("connect ShipStation", default=False)

    class Meta:
        verbose_name = "ShipStation settings"

    def __str__(self) -> str:
        return "ShipStation settings"


class ShippoSettings(SingletonModel):
    """A Shippo account. Its API token is an encrypted secret."""

    enabled = models.BooleanField("connect Shippo", default=False)

    class Meta:
        verbose_name = "Shippo settings"

    def __str__(self) -> str:
        return "Shippo settings"


SOURCES = {
    "shopify": "Shopify",
    "etsy": "Etsy",
    "ebay": "eBay",
    "woocommerce": "WooCommerce",
    "shipstation": "ShipStation",
    "shippo": "Shippo",
}


class ImportedOrder(models.Model):
    """An order brought in from an online store: its sale, or, while the store
    withheld the buyer's details, the order waiting for a customer (FR-IMP-3).
    The store's own order id is the key that keeps it from being imported twice
    (FR-IMP-2)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for a customer"
        IMPORTED = "imported", "Imported"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    source = models.CharField(max_length=20)
    external_order_id = models.CharField(max_length=64)
    order_number = models.CharField(max_length=50, blank=True)
    #: The platform's id for the buyer (FR-IMP-7), for deletion requests.
    buyer_id = models.CharField(max_length=64, blank=True, db_index=True)
    order_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    total = MoneyField(default=0)
    tax = MoneyField(default=0)
    shipping = MoneyField(default=0)
    financial_status = models.CharField(max_length=32, blank=True)
    carrier = models.CharField(max_length=60, blank=True)
    tracking = models.CharField(max_length=100, blank=True)
    shipped_on = models.DateField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    #: What staff should know: totals that don't add up, a refund, a cancellation.
    problem = models.TextField(blank=True)
    #: The order as the importer read it, to build the sale once it has a customer.
    payload = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invoice = models.OneToOneField(
        "billing.Invoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="imported_order",
    )
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    imported_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-order_date", "-imported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_order_id"], name="imported_order_once"
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_source_display()} order {self.order_number or self.external_order_id}"

    @property
    def payment_label(self) -> str:
        return self.financial_status.replace("_", " ")

    def get_source_display(self) -> str:
        return SOURCES.get(self.source, self.source.title())

    @property
    def hints(self) -> dict:
        """Where the buyer is, as far as the store said: often the state and country
        survive when the rest is withheld."""
        buyer = self.payload.get("customer") or {}
        return {k: buyer.get(k, "") for k in ("region", "country", "postal_code", "city")}


class DeletionRequest(models.Model):
    """A platform's request to delete a buyer's data (FR-IMP-6): an eBay
    marketplace account deletion notice, forwarded to the API by a relay or
    entered by hand.

    The buyer's details in the platform's stored orders are wiped when it
    arrives. Customer records are never changed automatically: one can also be
    a customer in their own right, so each waits for staff to anonymize it or
    dismiss the request. Sales and tax figures always stay.
    """

    class Status(models.TextChoices):
        OPEN = "open", "To review"
        DONE = "done", "Anonymized"
        DISMISSED = "dismissed", "Dismissed"
        NO_MATCH = "no_match", "Not one of your buyers"

    class Via(models.TextChoices):
        API = "api", "Forwarded to the API"
        HAND = "hand", "Entered by hand"
        EDGE = "edge", "Through the portal on Cloudflare"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    source = models.CharField(max_length=20, default="ebay")
    #: eBay's three ids for the account; the username is what orders carry.
    username = models.CharField(max_length=100, blank=True, db_index=True)
    user_id = models.CharField(max_length=100, blank=True)
    eias_token = models.CharField(max_length=200, blank=True)
    #: eBay's id for the notice: a notice delivered twice is handled once.
    notification_id = models.CharField(max_length=100, blank=True)
    event_date = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    via = models.CharField(max_length=8, choices=Via.choices, default=Via.HAND)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    #: How many stored orders had the buyer's details wiped on arrival.
    orders = models.PositiveIntegerField(default=0)
    #: Customers anonymized for this request.
    anonymized = models.JSONField(default=list, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "notification_id"],
                condition=~models.Q(notification_id=""),
                name="deletion_notice_once",
            )
        ]

    def __str__(self) -> str:
        who = self.username or self.user_id
        return f"{SOURCES.get(self.source, self.source)} deletion request for {who}"

    def get_source_display(self) -> str:
        return SOURCES.get(self.source, self.source)
