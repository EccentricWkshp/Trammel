"""What customers do on the edge portal (0.9.1), applied here: answering a quote
with their choices, the terms and a signature; sending files; and the online
stores' webhooks, callbacks and deletion notices relayed by the Worker."""

import base64
import json
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_edge import URL, FakeWorker, live, worker  # noqa: F401
from tests.test_portal import _sent_quote, grace, portal_on, smtp  # noqa: F401
from tests.test_quote_choices import _item
from trammel.agreements.models import AgreementSettings
from trammel.core import modules
from trammel.edge import publish, sync
from trammel.edge.models import EdgeAnswer, EdgeUpload
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.portal.models import PortalSettings, Upload
from trammel.quotes import services as quotes

D = Decimal
PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFUlEQVR42mNk"
    "YPhfz0AEYBxVSF+FAP5FDvcfRYWgAAAAAElFTkSuQmCC"
)


def _event(seq, kind, customer, data):
    return {
        "seq": seq,
        "kind": kind,
        "at": timezone.now().isoformat(),
        "ip": "203.0.113.9",
        "customer": str(customer.pk) if customer else None,
        "data": data,
    }


@pytest.fixture
def choice_quote(grace, admin):  # noqa: F811
    job = jobs.create_job(Job(title="Dining table", customer=grace))
    revision = quotes.create_quote(job)
    revision.deposit_kind, revision.deposit_value = "percent", D("50")
    revision.save()
    _item(revision, "Build and finish", "1000")
    walnut = _item(revision, "Walnut top", "800")
    maple = _item(revision, "Maple top", "500")
    bench = _item(revision, "Two benches", "900")
    quotes.set_offer(maple, "alternate", "Wood")
    quotes.set_offer(walnut, "alternate", "Wood", default=True)
    quotes.set_offer(bench, "optional")
    quotes.send(revision, actor=admin)
    revision.refresh_from_db()
    return revision, walnut, maple, bench


@pytest.mark.django_db
def test_a_quote_carries_what_answering_it_takes(live, choice_quote):  # noqa: F811
    revision, walnut, maple, bench = choice_quote
    sync.run()
    respond = live.of("quote")[str(revision.pk)]["data"]["respond"]
    assert respond["agreement"]["title"] == "Customer agreement" and respond["agreement"]["hash"]
    [wood] = respond["choices"]["groups"]
    assert wood["default"] == str(walnut.pk) and [x["id"] for x in wood["lines"]] == [
        str(walnut.pk),
        str(maple.pk),
    ]
    totals = respond["totals"]
    assert len(totals) == 4  # two woods, with or without the benches
    assert totals[str(walnut.pk)] == {"total": "$1,800.00", "deposit": "$900.00"}
    assert totals[",".join(sorted([str(maple.pk), str(bench.pk)]))]["total"] == "$2,400.00"


@pytest.mark.django_db
def test_an_approval_with_choices_and_a_signature_is_applied(live, grace, choice_quote):  # noqa: F811
    revision, walnut, maple, bench = choice_quote
    settings = AgreementSettings.load()
    settings.signature_enabled = True
    settings.save()
    sync.run()
    terms = live.of("quote")[str(revision.pk)]["data"]["respond"]["agreement"]["hash"]
    live.inbox = [
        _event(
            1,
            "quote_response",
            grace,
            {
                "revision": str(revision.pk),
                "answer": "approve",
                "name": "Grace Hopper",
                "comment": "Lovely",
                "terms": terms,
                "picks": [str(maple.pk), str(bench.pk)],
                "signature": PNG,
                "how": "signed in as grace@example.com",
                "email": "grace@example.com",
            },
        ),
    ]
    sync.run()
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.total == D("2400.00")
    approval = revision.approval
    assert approval.approved_by_name == "Grace Hopper" and approval.method == "portal"
    assert "In the portal on Cloudflare, signed in as grace@example.com." in approval.notes
    assert approval.signature  # the drawn signature, cleaned
    quote = live.of("quote")[str(revision.pk)]["data"]
    assert (
        quote["status"] == "Approved"
        and not quote["open"]
        and quote["approval"]["by"] == "Grace Hopper"
    )


@pytest.mark.django_db
def test_an_answer_that_cant_be_applied_is_explained_there(live, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    sync.run()
    stale = live.of("quote")[str(revision.pk)]["data"]["respond"]["agreement"]["hash"]
    # Revised meanwhile: the answer arrives at a superseded revision.
    quotes.revise(revision, actor=admin)
    live.inbox = [
        _event(
            1,
            "quote_response",
            grace,
            {
                "revision": str(revision.pk),
                "answer": "approve",
                "name": "Grace",
                "terms": stale,
                "picks": [],
                "signature": "",
                "how": "signed in",
            },
        ),
    ]
    sync.run()
    assert EdgeAnswer.objects.filter(revision=revision).exists()
    revision.refresh_from_db()
    assert revision.status == "superseded"
    # The superseded revision isn't shown; a declined one shows why it was refused.
    other = _sent_quote(grace, admin, title="Shelf")
    quotes.decline(other, actor=admin, reason="Changed their mind")
    EdgeAnswer.objects.create(revision=other, message="This quote could no longer be answered.")
    sync.run()
    assert live.of("quote")[str(other.pk)]["data"]["problem"].startswith("This quote")


@pytest.mark.django_db
def test_a_decline_is_applied(live, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    live.inbox = [
        _event(
            1,
            "quote_response",
            grace,
            {"revision": str(revision.pk), "answer": "decline", "comment": "Too dear"},
        ),
    ]
    sync.run()
    revision.refresh_from_db()
    assert revision.status == "declined"


@pytest.mark.django_db
def test_files_sent_there_are_taken_in_for_review(live, grace, admin, monkeypatch):  # noqa: F811
    modules.set_enabled("files", True)
    settings = PortalSettings.load()
    settings.uploads_enabled = True
    settings.save()
    job = jobs.create_job(Job(title="Sign", customer=grace))
    sync.run()
    site = live.records[("config", "site")]["data"]
    assert site["uploads"]["enabled"] and site["uploads"]["per_send"] >= 1
    png = base64.b64decode(PNG.split(",", 1)[1])
    live.objects["upload/u1/sketch.png"] = (png, "image/png")
    live.objects["upload/u2/notes.exe"] = (b"MZ\x90\x00", "application/octet-stream")

    def fetch(method, path, **kwargs):
        if method == "GET" and path.startswith("/_sync/objects/"):
            key = path.split("/_sync/objects/", 1)[1].replace("%2F", "/")
            return live.objects[key][0]
        return FakeWorker.call(live, method, path, **kwargs)

    monkeypatch.setattr("trammel.edge.client.call", fetch)
    ids = ["01900000-0000-7000-8000-0000000000f1", "01900000-0000-7000-8000-0000000000f2"]
    live.inbox = [
        _event(
            1,
            "upload",
            grace,
            {
                "id": ids[0],
                "key": "upload/u1/sketch.png",
                "name": "sketch.png",
                "size": len(png),
                "type": "image/png",
                "note": "Like this",
                "job": str(job.pk),
                "sent_by": "Grace",
            },
        ),
        _event(
            2,
            "upload",
            grace,
            {
                "id": ids[1],
                "key": "upload/u2/notes.exe",
                "name": "notes.exe",
                "size": 4,
                "type": "",
                "note": "",
                "job": None,
                "sent_by": "Grace",
            },
        ),
    ]
    sync.run()
    taken = EdgeUpload.objects.get(pk=ids[0])
    assert taken.upload is not None and taken.upload.job == job and taken.upload.note == "Like this"
    assert Upload.objects.filter(customer=grace, status="pending").count() == 1
    refused = EdgeUpload.objects.get(pk=ids[1])
    assert refused.upload is None and refused.reason
    # Gone from Cloudflare once taken; each shown to the customer with how it went.
    assert "upload/u1/sketch.png" not in live.objects and "upload/u2/notes.exe" not in live.objects
    shown = live.of("upload")
    assert shown[ids[0]]["data"]["status"] == "Waiting for us to look at it"
    assert shown[ids[0]]["job"] == str(job.pk)
    assert shown[ids[1]]["data"]["status"] == "Not taken"


@pytest.mark.django_db
def test_a_relayed_shopify_webhook_is_checked_here(live, monkeypatch):  # noqa: F811
    import hashlib
    import hmac

    from trammel.core import secrets, tasks
    from trammel.imports import shopify
    from trammel.imports.models import ShopifySettings

    modules.set_enabled("imports", True)
    config = ShopifySettings.load()
    config.enabled, config.webhooks_enabled, config.shop_domain = True, True, "shop.myshopify.com"
    config.save()
    secrets.put(shopify.API_SECRET_KEY, "whsec")
    queued = []
    monkeypatch.setattr(tasks, "enqueue", lambda name, payload=None, **kw: queued.append(payload))
    sync.run()
    assert live.records[("config", "site")]["data"]["hooks"]["shopify"] is True
    body = '{"id": 42}'
    good = base64.b64encode(hmac.new(b"whsec", body.encode(), hashlib.sha256).digest()).decode()
    live.inbox = [
        _event(
            1,
            "shopify_webhook",
            None,
            {"body": body, "hmac": "forged", "shop": "shop.myshopify.com"},
        ),
        _event(
            2, "shopify_webhook", None, {"body": body, "hmac": good, "shop": "shop.myshopify.com"}
        ),
    ]
    sync.run()
    assert queued == [{"order": {"id": 42}}]


@pytest.mark.django_db
def test_an_ebay_deletion_notice_needs_ebays_signature(live, monkeypatch):  # noqa: F811
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from trammel.imports import ebay
    from trammel.imports.models import DeletionRequest, EbaySettings

    modules.set_enabled("imports", True)
    config = EbaySettings.load()
    config.enabled = True
    config.save()
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # eBay hands its key out with the line breaks taken out.
    flat = pem.decode().replace("\n", "")
    monkeypatch.setattr(ebay, "public_key", lambda config, kid: ebay._pem(flat))
    body = json.dumps(
        {
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
            "notification": {
                "notificationId": "n-1",
                "eventDate": "2026-11-12T00:00:00.000Z",
                "data": {"username": "buyer1", "userId": "u1", "eiasToken": "e1"},
            },
        }
    )

    def signed(text):
        sig = key.sign(text.encode(), ec.ECDSA(hashes.SHA1()))  # noqa: S303
        return base64.b64encode(
            json.dumps({"kid": "k1", "signature": base64.b64encode(sig).decode()}).encode()
        ).decode()

    live.inbox = [
        _event(
            1,
            "ebay_deletion",
            None,
            {"body": body, "signature": signed(body.replace("buyer1", "other"))},
        ),
        _event(2, "ebay_deletion", None, {"body": body, "signature": signed(body)}),
    ]
    sync.run()
    rows = list(DeletionRequest.objects.all())
    assert [(r.username, r.via) for r in rows] == [("buyer1", "edge")]


@pytest.mark.django_db
def test_store_addresses_point_at_cloudflare_while_it_stands_in(live, admin_client):  # noqa: F811
    from trammel.imports.models import EbaySettings

    modules.set_enabled("imports", True)
    config = EbaySettings.load()
    config.enabled = True
    config.save()
    page = admin_client.get("/imports/settings/?tab=ebay").content.decode()
    assert f"{URL}/imports/ebay/callback/" in page
    assert "takes eBay's notices for you" in page
    page = admin_client.get("/imports/settings/?tab=etsy").content.decode()
    assert f"{URL}/imports/etsy/callback/" in page
    edge_page = admin_client.get("/settings/edge-portal/").content.decode()
    assert f"{URL}/imports/ebay/deletion/" in edge_page
    assert len(publish.items.__name__) and Client  # (imports kept for readers)
