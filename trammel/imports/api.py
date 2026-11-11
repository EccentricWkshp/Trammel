"""Online orders in the API: where a relay forwards eBay's account deletion
notices (FR-IMP-6).

eBay needs a public HTTPS address that answers its challenge and takes its
notices, which Trammel doesn't host. A small relay on the internet does that
and forwards each notice here, with an API key that holds only
"imports.deletion_notices". The relay is trusted to have checked eBay's
signature; Trammel only ever wipes the stored copy of an eBay buyer's details
on its own, and every customer record waits for staff.
"""

import datetime as dt

from django.core.exceptions import ValidationError
from ninja import Router, Schema

from trammel.core import access

from . import deletions
from .models import DeletionRequest

router = Router(tags=["imports"])


class NoticeData(Schema):
    username: str = ""
    userId: str = ""  # noqa: N815 — eBay's own field names
    eiasToken: str = ""  # noqa: N815


class Notification(Schema):
    notificationId: str = ""  # noqa: N815
    eventDate: dt.datetime | None = None  # noqa: N815
    data: NoticeData


class NoticeIn(Schema):
    """eBay's notice body, exactly as eBay sent it."""

    metadata: dict = {}
    notification: Notification


class NoticeOut(Schema):
    id: str
    status: str
    duplicate: bool
    orders: int


@router.post("/ebay/deletion-notices", response=NoticeOut)
def ebay_deletion_notice(request, payload: NoticeIn):
    """Take one eBay marketplace account deletion notice, as forwarded by a relay.

    The buyer's details in stored eBay orders are wiped at once; a customer the
    orders went to waits for review. A notice delivered again (same
    notificationId) is answered without doing anything twice.
    """
    access.require(request.actor, "imports.deletion_notices")
    topic = (payload.metadata or {}).get("topic", "MARKETPLACE_ACCOUNT_DELETION")
    if topic != "MARKETPLACE_ACCOUNT_DELETION":
        raise ValidationError(f"Not an account deletion notice ({topic}).")
    note = payload.notification
    row, is_new = deletions.receive(
        username=note.data.username,
        user_id=note.data.userId,
        eias_token=note.data.eiasToken,
        notification_id=note.notificationId,
        event_date=note.eventDate,
        via=DeletionRequest.Via.API,
    )
    return {"id": str(row.pk), "status": row.status, "duplicate": not is_new, "orders": row.orders}
