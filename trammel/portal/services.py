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
from dataclasses import dataclass

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from trammel.core import access, audit, modules
from trammel.customers.models import Customer

from .models import Access, Attempt, SignInCode

SESSION_KEY = "portal"
CHOICE_KEY = "portal_choice"
PENDING_KEY = "portal_pending"
#: A portal session lasts this long from signing in, however active.
SESSION_HOURS = 12

#: Rate limits (FR-PRT-6): codes asked for one address in 15 minutes; requests
#: from one IP address (an IPv6 /64) in an hour; wrong codes from one in an hour.
CODES_PER_ADDRESS = 3
REQUESTS_PER_IP = 10
FAILURES_PER_IP = 20
#: Wrong codes for one address in a day, from anywhere, before its codes stop
#: working until the day has passed: a six-digit code then can't be guessed.
FAILURES_PER_ADDRESS = 10


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
    live = SignInCode.objects.filter(
        email=email,
        used_at__isnull=True,
        expires_at__gt=timezone.now(),
        tries__lt=SignInCode.MAX_TRIES,
    )
    ids = list(live.values_list("pk", flat=True))
    counted = list(
        SignInCode.objects.filter(pk__in=ids, tries__lt=SignInCode.MAX_TRIES).values_list(
            "pk", flat=True
        )
    )
    SignInCode.objects.filter(pk__in=counted, tries__lt=SignInCode.MAX_TRIES).update(
        tries=F("tries") + 1
    )
    match = None
    if len(code) == 6:
        wanted = SignInCode.hash(email, code)
        match = next(
            (
                row
                for row in SignInCode.objects.filter(pk__in=counted)
                if hmac.compare_digest(row.code_hash, wanted)
            ),
            None,
        )
    if match is None:
        _record(Attempt.Kind.VERIFY, email, ip)
        return []
    used = SignInCode.objects.filter(pk=match.pk, used_at__isnull=True).update(
        used_at=timezone.now()
    )
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


def sign_in(request, customer: Customer, email: str) -> None:
    email = normalize(email)
    request.session.cycle_key()
    request.session.pop(CHOICE_KEY, None)
    request.session.pop(PENDING_KEY, None)
    name = name_for(customer, email)
    request.session[SESSION_KEY] = {
        "customer": str(customer.pk),
        "email": email,
        "name": name,
        "since": timezone.now().isoformat(),
    }
    Access.objects.filter(customer=customer).update(last_sign_in_at=timezone.now())
    audit.record(
        "portal.sign_in",
        customer,
        summary=f"{name} ({email}) signed in to the portal",
        actor=_actor(customer, name),
    )


def sign_out(request) -> None:
    for key in (SESSION_KEY, CHOICE_KEY, PENDING_KEY):
        request.session.pop(key, None)


def _actor(customer: Customer, name: str) -> access.Actor:
    label = name if name == customer.name else f"{name} ({customer.name})"
    return access.Actor(kind="customer", customer_id=customer.pk, label=f"{label}, in the portal")


def current(request) -> Visitor | None:
    """The signed-in customer, checked afresh: the portal on, the session not too
    old, and the address still allowed to sign in for that customer."""
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
    customer = next(
        (c for c in customers_for(data.get("email", "")) if str(c.pk) == data.get("customer")),
        None,
    )
    if customer is None:
        sign_out(request)
        return None
    return Visitor(
        customer=customer,
        email=data["email"],
        name=data.get("name") or customer.name,
        actor=_actor(customer, data.get("name") or customer.name),
    )


def set_access(customer: Customer, enabled: bool, *, user) -> Access:
    """Turn a customer's portal access on or off (staff)."""
    row, _ = Access.objects.get_or_create(customer=customer)
    if row.enabled != enabled:
        row.enabled = enabled
        row.changed_at = timezone.now()
        row.changed_by = user
        row.save()
        audit.record(
            "portal.access_on" if enabled else "portal.access_off",
            customer,
            summary=f"{'Gave' if enabled else 'Took away'} portal access for {customer}",
        )
    return row


def link(path: str = "") -> str:
    """An absolute link into the portal, for emails."""
    from django.urls import reverse

    return settings.BASE_URL + (path or reverse("portal:home"))


def has_access(customer) -> bool:
    return Access.objects.filter(customer=customer, enabled=True).exists()


def prune(older_than_days: int = 30) -> int:
    cutoff = _since(days=older_than_days)
    removed = Attempt.objects.filter(at__lt=cutoff).delete()[0]
    removed += SignInCode.objects.filter(created_at__lt=cutoff).delete()[0]
    return removed
