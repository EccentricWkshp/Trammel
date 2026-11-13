"""The edge portal's settings and what has been published to it (SPEC §12, 0.9.x)."""

from __future__ import annotations

from django.db import models

from trammel.core.models import SingletonModel


class EdgeSettings(SingletonModel):
    """Where the edge portal is on Cloudflare, and how its sync is doing. The API
    token and the sync key are kept encrypted (core.secrets), not here."""

    account_id = models.CharField(
        "Cloudflare account ID",
        max_length=32,
        blank=True,
        help_text="Shown in Cloudflare's dashboard on the account's home page and on Workers "
        "& Pages. Filled in for you when the token can list its accounts and reaches only one.",
    )
    account_name = models.CharField(max_length=200, blank=True, editable=False)
    hostname = models.CharField(
        "custom address",
        max_length=253,
        blank=True,
        help_text="Such as portal.example.com, on a domain whose DNS is on Cloudflare. "
        "Leave it blank for the account's free workers.dev address.",
    )
    keep_closed_days = models.PositiveSmallIntegerField(
        "keep closed jobs for (days)",
        default=90,
        help_text="A closed, declined or canceled job, and everything with it, comes off "
        "Cloudflare this many days after it last changed. 0 takes it off at once.",
    )
    worker_name = models.CharField(max_length=63, default="trammel-portal", editable=False)
    database_id = models.CharField(max_length=64, blank=True, editable=False)
    bucket = models.CharField(max_length=63, blank=True, editable=False)
    public_url = models.URLField(blank=True, editable=False)
    deployed_version = models.CharField(max_length=40, blank=True, editable=False)
    deployed_at = models.DateTimeField(null=True, blank=True, editable=False)
    #: Deploying, as a background task: its steps so far, and whether it's done.
    deploying = models.BooleanField(default=False, editable=False)
    deploy_log = models.JSONField(default=list, editable=False)
    #: Serving customers: Trammel's own portal pages are off while this is on.
    active = models.BooleanField(default=False, editable=False)
    #: Deployed, but its address didn't answer from here yet (a new address can
    #: take a while to reach every DNS server): it goes live when it does.
    awaiting_address = models.BooleanField(default=False, editable=False)
    #: The last inbox event applied.
    inbox_after = models.BigIntegerField(default=0, editable=False)
    #: A hash of the Shopify webhook secret the Worker was last given, so a
    #: changed one is sent again (SEC-08, 0.10.6).
    hook_secret_hash = models.CharField(max_length=64, blank=True, editable=False)
    #: Which Worker database this is (it says): a new one gets everything again.
    instance = models.CharField(max_length=64, blank=True, editable=False)
    #: Inbox events that failed, by number: how many times, until one is skipped.
    failures = models.JSONField(default=dict, editable=False)
    #: The bot check on signing in (Cloudflare Turnstile): its site key; the
    #: secret is kept encrypted.
    turnstile_sitekey = models.CharField(max_length=100, blank=True, editable=False)
    #: A bot check made by hand in Cloudflare's dashboard (an account API token
    #: can't make one): its site key; its secret key is kept encrypted.
    turnstile_own_sitekey = models.CharField(
        "Turnstile site key",
        max_length=100,
        blank=True,
        help_text="Only with an account API token, which can't set up the bot check itself: "
        "make a Turnstile widget in Cloudflare's dashboard for the portal's address and paste "
        "its keys here. Leave blank with a user token that has the Turnstile permission.",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    last_error_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "edge portal settings"

    def __str__(self) -> str:
        return "Edge portal settings"


class Published(models.Model):
    """A record on the edge, and the files stored with it, so a change is pushed
    once and something no longer shown is taken down."""

    type = models.CharField(max_length=20)
    key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    objects_stored = models.JSONField(default=list)
    pushed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["type", "key"], name="edge_published_unique")
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.key}"


class EdgeUpload(models.Model):
    """A file a customer sent from the edge portal (FR-PRT-7), and what became of
    it: taken in for review, or not taken and why. Its id is the Worker's, so
    what the customer sees there is updated in place."""

    id = models.UUIDField(primary_key=True)
    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE, related_name="+")
    job = models.ForeignKey("jobs.Job", null=True, blank=True, on_delete=models.SET_NULL)
    upload = models.ForeignKey(
        "portal.Upload", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    name = models.CharField(max_length=255)
    reason = models.CharField(max_length=500, blank=True)
    at = models.DateTimeField()

    def __str__(self) -> str:
        return self.name


class EdgeAnswer(models.Model):
    """A customer's answer to a quote from the edge portal that couldn't be
    applied, and why: shown to them there (FR-PRT-3)."""

    revision = models.ForeignKey("quotes.QuoteRevision", on_delete=models.CASCADE, related_name="+")
    message = models.CharField(max_length=500)
    at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.message


class EdgeAccessCode(models.Model):
    """A code staff made for a customer (FR-PRT-10), hashed with the edge's sync
    key so the Worker can check it. Made with the code, the one time it's known."""

    code = models.OneToOneField("portal.AccessCode", on_delete=models.CASCADE, related_name="edge")
    code_hash = models.CharField(max_length=64)

    def __str__(self) -> str:
        return f"Edge copy of {self.code}"
