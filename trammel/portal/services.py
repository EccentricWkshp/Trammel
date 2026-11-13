"""Customer portal sign-in and session (FR-PRT-1, FR-PRT-6).

A customer signs in with a six-digit code emailed to an address on file: the
customer's own, or one of their contacts'. Staff turn the portal on for each
customer; nobody can sign in until they do. Codes expire in minutes, work once,
die after a few wrong tries, and both asking for them and trying them are
rate-limited by address and by IP address.

The signed-in customer is kept in the session, apart from any staff sign-in in
the same browser, and checked again on every request: switching the portal off,
or a customer's access, ends their session at once.
"""

from __future__ import annotations

import datetime as dt
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from trammel.core import access, audit, modules
from trammel.customers.models import Customer

from .models import Access, AccessCode, Attempt, SignInCode

SESSION_KEY = "portal"
CHOICE_KEY = "portal_choice"
PENDING_KEY = "portal_pending"
#: Staff viewing the portal as a customer would: kept apart from a customer's
#: own portal session, and from the staff sign-in it rides on.
PREVIEW_KEY = "portal_preview"
#: A portal session lasts this long from signing in, however active.
SESSION_HOURS = 12
#: A preview lasts this long from starting it.
PREVIEW_HOURS = 2

#: Rate limits (FR-PRT-6): codes asked for one address in 15 minutes; requests
#: from one IP address (an IPv6 /64) in an hour; wrong codes from one in an hour.
CODES_PER_ADDRESS = 3
REQUESTS_PER_IP = 10
FAILURES_PER_IP = 20
#: Wrong codes for one address in a day, from anywhere, before its codes stop
#: working until the day has passed: a six-digit code then can't be guessed.
FAILURES_PER_ADDRESS = 10


#: Where customers are served instead of here, if anywhere: each fn() returns
#: that portal's public address, or "". The edge portal registers one (0.9.x);
#: while it serves customers, these pages are off (staff previews aside).
elsewhere: list[Callable[[], str]] = []
#: Called with (the AccessCode row, the code) when staff make a code: the one
#: time the code itself is known, for a portal elsewhere to keep its own hash.
on_code_made: list[Callable] = []


def served_elsewhere() -> str:
    """The address of the portal serving customers instead of this one, or ""."""
    for fn in elsewhere:
        if url := fn():
            return url
    return ""


def is_on() -> bool:
    """The portal is on: its module enabled, and not forced off (FR-PRT-5, FR-PRT-8)."""
    return modules.is_enabled("portal")


def email_ready() -> bool:
    """Codes can be sent: email is set up (FR-PRT-1)."""
    from trammel.notifications.models import SmtpSettings

    return SmtpSettings.load().is_configured or bool(getattr(settings, "TRAMMEL_EMAIL_BACKEND", ""))


def normalize(email: str) -> str:
    return (email or "").strip().lower()[:254]


def customers_for(email: str) -> list[Customer]:
    """The customers an address may sign in for: theirs, or a contact's, with
    portal access turned on."""
    email = normalize(email)
    if not email:
        return []
    return list(
        Customer.objects.filter(
            portal_access__enabled=True, is_placeholder=False, is_internal=False
        )
        .filter(
            Q(email__iexact=email)
            | Q(contacts__email__iexact=email, contacts__deleted_at__isnull=True)
        )
        .distinct()
        .order_by("name")
    )


def name_for(customer: Customer, email: str) -> str:
    """Who an address is at a customer: the contact's name, or the customer's."""
    contact = customer.contacts.filter(email__iexact=email).first()
    return contact.name if contact else customer.name


def signers_of(customer: Customer) -> list[tuple[str, str]]:
    """(name, address) of everyone who can sign in for a customer."""
    out = []
    if customer.email:
        out.append((customer.name, customer.email.lower()))
    for contact in customer.contacts.exclude(email=""):
        if contact.email.lower() not in {e for _, e in out}:
            out.append((contact.name, contact.email.lower()))
    return out


def _since(**delta) -> dt.datetime:
    return timezone.now() - dt.timedelta(**delta)


def ip_key(ip) -> str:
    """What rate limits count an IP address as: itself, or for IPv6 its /64
    network, since one connection usually has a whole /64 to rotate through."""
    import ipaddress

    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return ""
    if address.version == 6 and address.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address.ipv4_mapped or address) if address.version == 6 else str(address)


def _record(kind: str, email: str, ip, succeeded: bool = False) -> None:
    Attempt.objects.create(
        kind=kind, email=email, ip=ip or None, ip_key=ip_key(ip), succeeded=succeeded
    )


def _failures(**filters) -> int:
    return Attempt.objects.filter(kind=Attempt.Kind.VERIFY, succeeded=False, **filters).count()


def address_locked(email: str) -> bool:
    """Too many wrong codes for this address lately, from anywhere: codes for it
    are neither sent nor accepted until the day has passed (FR-PRT-6)."""
    return _failures(email=email, at__gte=_since(hours=24)) >= FAILURES_PER_ADDRESS


def request_code(email: str, ip) -> None:
    """Email a sign-in code, if the address may sign in and nothing is rate
    limited. Says nothing either way, so addresses can't be probed."""
    email = normalize(email)
    if not email:
        return
    key = ip_key(ip)
    requests = Attempt.objects.filter(kind=Attempt.Kind.REQUEST)
    too_many = (
        requests.filter(email=email, at__gte=_since(minutes=15)).count() >= CODES_PER_ADDRESS
        or bool(
            key and requests.filter(ip_key=key, at__gte=_since(hours=1)).count() >= REQUESTS_PER_IP
        )
        or address_locked(email)
    )
    _record(Attempt.Kind.REQUEST, email, ip)
    if too_many or not is_on():
        return
    customers = customers_for(email)
    if not customers:
        return
    from trammel.notifications.services import notify

    # Earlier codes still waiting keep working until they expire, so asking for
    # codes in someone else's name never cancels the one they're about to type.
    code = f"{secrets.randbelow(1_000_000):06d}"
    SignInCode.objects.create(
        email=email,
        code_hash=SignInCode.hash(email, code),
        expires_at=timezone.now() + dt.timedelta(minutes=SignInCode.LIFETIME_MINUTES),
        ip=ip or None,
    )
    notify(
        "portal_code",
        to=[email],
        entity=customers[0],
        context={"code": code, "minutes": SignInCode.LIFETIME_MINUTES},
    )
    audit.record(
        "portal.code_sent",
        customers[0],
        summary=f"Sent a portal sign-in code to {email}",
        actor=access.ANONYMOUS,
    )


def _claim_tries(email: str) -> list[tuple]:
    """Count this try against each of `email`'s waiting codes, in one statement,
    and return (id, hash) for just the codes it counted against: one still
    unused, unexpired and under its tries when this statement reached it.
    PostgreSQL rechecks those conditions on a row another request has just
    changed, so guesses at the same moment can't share a try (SEC-06, 0.10.5)."""
    from django.db import connection

    table = SignInCode._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET tries = tries + 1 "  # noqa: S608 — the model's own table
            "WHERE email = %s AND used_at IS NULL AND expires_at > %s AND tries < %s "
            "RETURNING id, code_hash",
            [email, timezone.now(), SignInCode.MAX_TRIES],
        )
        return list(cursor.fetchall())


def verify_code(email: str, code: str, ip) -> list[Customer]:
    """The customers `email` may sign in for, if `code` is one of its codes still
    waiting — which is then used up. Otherwise an empty list.

    Every try is counted against each waiting code before any is compared, in
    one atomic update, so guesses made in parallel can't share a try.
    """
    email = normalize(email)
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    key = ip_key(ip)
    if (key and _failures(ip_key=key, at__gte=_since(hours=1)) >= FAILURES_PER_IP) or (
        address_locked(email)
    ):
        _record(Attempt.Kind.VERIFY, email, ip)
        return []
    claimed = _claim_tries(email)
    match = None
    if len(code) == 6:
        wanted = SignInCode.hash(email, code)
        match = next(
            (pk for pk, digest in claimed if hmac.compare_digest(digest, wanted)),
            None,
        )
    if match is None:
        _record(Attempt.Kind.VERIFY, email, ip)
        return []
    used = SignInCode.objects.filter(pk=match, used_at__isnull=True).update(used_at=timezone.now())
    customers = customers_for(email) if used and is_on() else []
    _record(Attempt.Kind.VERIFY, email, ip, succeeded=bool(customers))
    return customers


# ---------------------------------------------------------------------------
# The portal session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Visitor:
    """The customer signed in to the portal, and who at the customer it is."""

    customer: Customer
    email: str
    name: str
    actor: access.Actor
    #: The staff member viewing the portal as the customer would, read-only;
    #: empty for the customer themselves.
    viewer: str = ""
    #: Who gave the customer the code they signed in with, if staff did.
    given_by: str = ""

    @property
    def is_preview(self) -> bool:
        return bool(self.viewer)

    @property
    def how(self) -> str:
        """How they signed in, for the record of what they do."""
        if self.given_by:
            return f"signed in with a code from {self.given_by}"
        return f"signed in as {self.email}"


def sign_in(request, customer: Customer, email: str) -> None:
    email = normalize(email)
    name = name_for(customer, email)
    _start(request, {"customer": str(customer.pk), "email": email, "name": name})
    Access.objects.filter(customer=customer).update(last_sign_in_at=timezone.now())
    audit.record(
        "portal.sign_in",
        customer,
        summary=f"{name} ({email}) signed in to the portal",
        actor=_actor(customer, name),
    )


def _start(request, data: dict) -> None:
    request.session.cycle_key()
    request.session.pop(CHOICE_KEY, None)
    request.session.pop(PENDING_KEY, None)
    request.session[SESSION_KEY] = {**data, "since": timezone.now().isoformat()}


def sign_out(request) -> None:
    for key in (SESSION_KEY, CHOICE_KEY, PENDING_KEY):
        request.session.pop(key, None)


def _actor(customer: Customer, name: str) -> access.Actor:
    label = name if name == customer.name else f"{name} ({customer.name})"
    return access.Actor(kind="customer", customer_id=customer.pk, label=f"{label}, in the portal")


def can_preview(actor, customer: Customer) -> bool:
    """Staff may view the portal as this customer would: the portal on, a real
    customer, and both seeing the customer and managing portal access. Managing
    access is asked for because the portal shows the customer's quotes and
    invoices, which a staff role may not otherwise see."""
    return (
        is_on()
        and not (customer.is_internal or customer.is_placeholder)
        and access.has_perm(actor, "portal.manage")
        and access.has_perm(actor, "customers.view", customer)
    )


def start_preview(request, customer: Customer) -> None:
    """View the portal as the customer would, read-only, until stopped."""
    request.session[PREVIEW_KEY] = {
        "customer": str(customer.pk),
        "user": str(request.user.pk),
        "since": timezone.now().isoformat(),
    }
    audit.record(
        "portal.preview",
        customer,
        summary=f"{request.user.display_name} viewed the portal as {customer} sees it",
    )


def stop_preview(request) -> str | None:
    """End a preview; the customer it was of, if any."""
    data = request.session.pop(PREVIEW_KEY, None) or {}
    return data.get("customer")


def _preview(request) -> Visitor | None:
    """The customer staff are viewing the portal as, checked afresh: the same
    staff member still signed in and allowed to, and the preview not too old."""
    data = request.session.get(PREVIEW_KEY)
    if not data:
        return None
    user = getattr(request, "user", None)
    try:
        since = dt.datetime.fromisoformat(data["since"])
        customer = Customer.objects.get(pk=data["customer"])
    except KeyError, TypeError, ValueError, Customer.DoesNotExist:
        since = customer = None
    if (
        customer is None
        or not (user and user.is_authenticated and str(user.pk) == data.get("user"))
        or timezone.now() - since > dt.timedelta(hours=PREVIEW_HOURS)
        or not can_preview(access.user_actor(user), customer)
    ):
        request.session.pop(PREVIEW_KEY, None)
        return None
    return Visitor(
        customer=customer,
        email=customer.email.lower(),
        name=customer.name,
        actor=_actor(customer, customer.name),
        viewer=user.display_name,
    )


def current(request) -> Visitor | None:
    """The signed-in customer, checked afresh: the portal on, the session not too
    old, and the address still allowed to sign in for that customer. Staff
    viewing the portal as a customer would come first."""
    if preview := _preview(request):
        return preview
    data = request.session.get(SESSION_KEY)
    if not data:
        return None
    try:
        since = dt.datetime.fromisoformat(data["since"])
    except KeyError, TypeError, ValueError:
        since = None
    if since is None or timezone.now() - since > dt.timedelta(hours=SESSION_HOURS) or not is_on():
        sign_out(request)
        return None
    if data.get("given_by"):
        # Signed in with a code from staff: the customer must still have access.
        customer = _with_access().filter(pk=data.get("customer")).first()
    else:
        customer = next(
            (c for c in customers_for(data.get("email", "")) if str(c.pk) == data.get("customer")),
            None,
        )
    if customer is None:
        sign_out(request)
        return None
    return Visitor(
        customer=customer,
        email=data.get("email", ""),
        name=data.get("name") or customer.name,
        actor=_actor(customer, data.get("name") or customer.name),
        given_by=data.get("given_by", ""),
    )


def _with_access():
    return Customer.objects.filter(
        portal_access__enabled=True, is_placeholder=False, is_internal=False
    )


# ---------------------------------------------------------------------------
# Codes staff give out (FR-PRT-10)
# ---------------------------------------------------------------------------


def waiting_code(customer: Customer) -> AccessCode | None:
    """The customer's code from staff that can still be used, if any."""
    return next(
        (
            row
            for row in customer.portal_codes.filter(used_at=None, canceled_at=None)
            .select_related("created_by")
            .order_by("-created_at")
            if row.is_live
        ),
        None,
    )


def cancel_codes(customer: Customer, *, why: str = "") -> int:
    """Cancel the customer's codes from staff that are still waiting."""
    canceled = customer.portal_codes.filter(
        used_at=None, canceled_at=None, expires_at__gt=timezone.now()
    ).update(canceled_at=timezone.now())
    if canceled and why:
        audit.record("portal.code_canceled", customer, summary=why)
    return canceled


def make_code(customer: Customer, *, user) -> tuple[str, AccessCode]:
    """A new one-time code for staff to give the customer, replacing any still
    waiting. The code itself is returned this once and never kept."""
    if not (is_on() and has_access(customer)):
        raise ValueError("Turn on the customer's portal access first.")
    cancel_codes(customer)
    raw = "".join(secrets.choice(AccessCode.ALPHABET) for _ in range(AccessCode.LENGTH))
    row = AccessCode.objects.create(
        customer=customer,
        code_hash=AccessCode.hash(raw),
        created_by=user,
        expires_at=timezone.now() + dt.timedelta(days=AccessCode.LIFETIME_DAYS),
    )
    for fn in on_code_made:
        fn(row, raw)
    audit.record(
        "portal.code_made",
        customer,
        summary=f"Made a portal sign-in code for {customer}, good until "
        f"{timezone.localtime(row.expires_at):%m/%d/%Y}",
    )
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4)), row


def use_code(request, code: str, ip) -> Customer | None:
    """Sign in with a code from staff, which is then used up. Wrong codes count
    toward the same limit on tries from one IP address as emailed codes."""
    code = AccessCode.normalize(code)
    key = ip_key(ip)
    if not is_on() or (key and _failures(ip_key=key, at__gte=_since(hours=1)) >= FAILURES_PER_IP):
        _record(Attempt.Kind.VERIFY, "", ip)
        return None
    row = None
    if len(code) == AccessCode.LENGTH:
        row = (
            AccessCode.objects.filter(
                code_hash=AccessCode.hash(code),
                used_at=None,
                canceled_at=None,
                expires_at__gt=timezone.now(),
                customer__in=_with_access(),
            )
            .select_related("customer", "created_by")
            .first()
        )
    # Claimed in one update, so the same code can't sign in twice at once.
    if row is None or not AccessCode.objects.filter(pk=row.pk, used_at=None).update(
        used_at=timezone.now()
    ):
        _record(Attempt.Kind.VERIFY, "", ip)
        return None
    _record(Attempt.Kind.VERIFY, "", ip, succeeded=True)
    customer = row.customer
    given_by = row.created_by.display_name if row.created_by else "staff"
    _start(
        request,
        {
            "customer": str(customer.pk),
            "email": "",
            "name": customer.name,
            "given_by": given_by,
        },
    )
    Access.objects.filter(customer=customer).update(last_sign_in_at=timezone.now())
    audit.record(
        "portal.sign_in",
        customer,
        summary=f"{customer} signed in to the portal with a code from {given_by}",
        actor=_actor(customer, customer.name),
    )
    return customer


def set_access(customer: Customer, enabled: bool, *, user) -> Access:
    """Turn a customer's portal access on or off (staff)."""
    row, _ = Access.objects.get_or_create(customer=customer)
    if row.enabled != enabled:
        row.enabled = enabled
        row.changed_at = timezone.now()
        row.changed_by = user
        row.save()
        if not enabled:
            cancel_codes(customer)
        audit.record(
            "portal.access_on" if enabled else "portal.access_off",
            customer,
            summary=f"{'Gave' if enabled else 'Took away'} portal access for {customer}",
        )
    return row


def link(path: str = "") -> str:
    """An absolute link into the portal, for emails."""
    from django.urls import reverse

    base = served_elsewhere() or settings.BASE_URL
    return base.rstrip("/") + (path or reverse("portal:home"))


def has_access(customer) -> bool:
    return Access.objects.filter(customer=customer, enabled=True).exists()


def prune(older_than_days: int = 30) -> int:
    cutoff = _since(days=older_than_days)
    removed = Attempt.objects.filter(at__lt=cutoff).delete()[0]
    removed += SignInCode.objects.filter(created_at__lt=cutoff).delete()[0]
    removed += AccessCode.objects.filter(expires_at__lt=cutoff).delete()[0]
    return removed
