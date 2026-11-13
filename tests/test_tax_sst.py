"""SST files in the shape Washington publishes them: ZIP archives, by upload or URL.

The records below are copied from WA DOR's WAR2026Q4 (rates) and WAB2026Q4
(boundaries) files, trimmed to a few lines.
"""

import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from trammel.core import outbound, tasks
from trammel.tax import importers, services
from trammel.tax.models import Boundary, ImportBatch, Jurisdiction, TaxRate

ON = dt.date(2026, 11, 15)

RATES = (
    "\n".join(
        [
            "53,45,53,0.06500,0.06500,0.06500,0.06500,19830301,99991231",
            "53,00,053,0.03100,0.03100,0.03100,0.03100,20260701,20271231",
            "53,00,061,0.02800,0.02800,0.02800,0.02800,20220401,99991231",
            "53,01,70000,0.04000,0.04000,0.04000,0.04000,20260701,20330331",
            "53,63,L2717,0.04000,0.04000,0.04000,0.04000,20260701,20330331",
            "53,63,L3131,0.01200,0.01200,0.01200,0.01200,20220401,99991231",
        ]
    )
    + "\n"
)


def boundary(kind, begin, end, zl, el, zh, eh, ser, county="", place="", district=None):
    f = [""] * 90
    f[0], f[1], f[2] = kind, begin, end
    f[17], f[18], f[19], f[20], f[21] = zl, el, zh, eh, ser
    f[22] = f[23] = "53"
    f[24], f[25] = county, place
    if district:
        f[29], f[30], f[31] = "ST", district, "45"  # WA tags districts type 45 here
    return ",".join(f)


BOUNDARIES = (
    "\n".join(
        [
            # Tacoma 98402: the whole ZIP via location code L2717 …
            boundary(
                "Z", "20261001", "99991231", "98402", "", "98402", "", "09204", district="L2717"
            ),
            # … and a ZIP+4 range via the city (FIPS place 70000).
            boundary(
                "4",
                "20110401",
                "99991231",
                "98402",
                "0000",
                "98402",
                "0100",
                "02717",
                place="70000",
            ),
            # Snohomish County, unincorporated, with an additive district: 2.8 % + 1.2 %.
            boundary(
                "Z",
                "20220401",
                "99991231",
                "98087",
                "",
                "98087",
                "",
                "09049",
                county="061",
                district="L3131",
            ),
            # An expired record, long gone: skipped.
            boundary("Z", "20100101", "20111231", "98087", "", "98087", "", "09049", county="061"),
            # Street-address records are skipped.
            "A,20210101,99991231,1,99,B,,MAIN,ST,,,,,,TACOMA,98402,1000" + "," * 73,
        ]
    )
    + "\n"
)


def zipped(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def import_bytes(data: bytes, name: str, tmp_path) -> ImportBatch:
    path = tmp_path / name
    path.write_bytes(data)
    return importers.import_sst_path(path, filename=name)


def test_detect():
    assert importers.detect(RATES.splitlines()[0]) == "rates"
    assert importers.detect(BOUNDARIES.splitlines()[0]) == "boundaries"
    assert importers.detect("postal_code,rate") is None


@pytest.mark.django_db
def test_washington_files_resolve_tacoma_and_snohomish(setup_done, tmp_path):
    rates = import_bytes(zipped("WAR2026Q4.csv", RATES), "WAR2026Q4.zip", tmp_path)
    bounds = import_bytes(zipped("WAB2026Q4.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)
    assert (rates.kind, rates.rows) == ("rates", 6)
    assert (bounds.kind, bounds.rows, bounds.skipped) == ("boundaries", 3, 2)

    tacoma = services.lookup_postal("98402", ON)
    assert tacoma.total_rate == Decimal("10.5") and tacoma.location_code == "2717"
    by_city = services.lookup_postal("98402-0050", ON)  # the ZIP+4 record wins
    assert by_city.total_rate == Decimal("10.5") and by_city.location_code == "2717"
    snohomish = services.lookup_postal("98087", ON)
    assert snohomish.total_rate == Decimal("10.5")  # 6.5 + 2.8 + 1.2
    assert {c.code for c in snohomish.components} == {
        "SST-53-45-53",
        "SST-53-00-061",
        "SST-53-D-L3131",
    }


@pytest.mark.django_db
def test_a_new_quarter_replaces_the_last(setup_done, tmp_path):
    q3 = import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q3.zip", tmp_path)
    q4 = import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)
    assert Boundary.objects.filter(source="sst").count() == 3
    assert set(Boundary.objects.values_list("batch", flat=True)) == {q4.pk}
    import_bytes(RATES.encode(), "WAR2026Q3.csv", tmp_path)
    # The next quarter drops the L3131 district: its jurisdiction goes with it.
    fewer = "\n".join(line for line in RATES.splitlines() if "L3131" not in line) + "\n"
    rates_q4 = import_bytes(fewer.encode(), "WAR2026Q4.csv", tmp_path)
    assert TaxRate.objects.filter(source="sst").count() == 5
    assert set(TaxRate.objects.values_list("batch", flat=True)) == {rates_q4.pk}
    assert not Jurisdiction.objects.filter(code="SST-53-D-L3131").exists()
    statuses = dict(ImportBatch.objects.values_list("filename", "status"))
    assert statuses == {
        "WAB2026Q3.zip": "replaced",
        "WAB2026Q4.zip": "done",
        "WAR2026Q3.csv": "replaced",
        "WAR2026Q4.csv": "done",
    }
    q3.refresh_from_db()
    assert q3.rows == 3  # what it brought in is kept for the record


@pytest.mark.django_db
def test_rates_without_a_boundary_file_say_so(admin_client, tmp_path):
    import_bytes(RATES.encode(), "WAR2026Q4.csv", tmp_path)
    looked = services.lookup_postal("98402", ON)
    assert not looked.resolved and "B file" in looked.note
    assert "no SST boundary file is" in admin_client.get("/tax/").content.decode()
    import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)
    assert services.lookup_postal("98402", ON).resolved
    assert "no SST boundary file is" not in admin_client.get("/tax/").content.decode()


@pytest.mark.django_db
def test_removing_an_import_takes_everything_it_brought(admin_client, tmp_path, customer):
    from trammel.billing import services as billing
    from trammel.billing.models import Invoice

    rates = import_bytes(RATES.encode(), "WAR2026Q4.csv", tmp_path)
    bounds = import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)
    # An invoice that chose a jurisdiction by hand keeps it.
    draft = billing.create(customer, kind=Invoice.Kind.DIRECT_SALE)
    draft.manual_jurisdiction = Jurisdiction.objects.get(code="SST-53-01-70000")
    draft.save()

    page = admin_client.get(f"/tax/imports/{rates.pk}/remove/").content.decode()
    assert "6 rate(s)" in page and "Remove" in page
    assert admin_client.post(f"/tax/imports/{rates.pk}/remove/").status_code == 302
    assert not TaxRate.objects.exists()
    assert list(Jurisdiction.objects.values_list("code", flat=True)) == ["SST-53-01-70000"]
    assert Boundary.objects.count() == 3  # the boundary import is its own

    admin_client.post(f"/tax/imports/{bounds.pk}/remove/")
    assert not Boundary.objects.exists() and not ImportBatch.objects.exists()
    from trammel.core.models import AuditEvent

    assert AuditEvent.objects.filter(action="tax.import_removed").count() == 2


@pytest.mark.django_db
def test_a_running_import_cannot_be_removed(admin_client):
    batch = ImportBatch.objects.create(source="sst", filename="WAB.zip", status="running")
    admin_client.post(f"/tax/imports/{batch.pk}/remove/")
    assert ImportBatch.objects.filter(pk=batch.pk).exists()


@pytest.mark.django_db
def test_removing_rates_in_bulk(admin_client, tmp_path):
    import_bytes(RATES.encode(), "WAR2026Q4.csv", tmp_path)
    by_hand = Jurisdiction.objects.create(code="9999", name="By hand")
    manual = TaxRate.objects.create(
        jurisdiction=by_hand, rate=Decimal("8"), effective_from=dt.date(2026, 1, 1)
    )
    county = TaxRate.objects.get(jurisdiction__code="SST-53-00-053")

    # Ticked: a confirmation first, then the removal.
    ticked = {"rate": [str(county.pk), str(manual.pk), "not-a-uuid"]}
    page = admin_client.post("/tax/rates/remove/", ticked).content.decode()
    assert "Remove 2 rate(s)?" in page and TaxRate.objects.count() == 7
    admin_client.post("/tax/rates/remove/", {**ticked, "confirm": "1"})
    assert TaxRate.objects.count() == 5
    # The imported jurisdiction goes with its last rate; the one entered by hand stays.
    assert not Jurisdiction.objects.filter(code="SST-53-00-053").exists()
    assert Jurisdiction.objects.filter(code="9999").exists()

    # Everything a search shows.
    listing = admin_client.get("/tax/?q=SST-53-D&source=sst").content.decode()
    assert "Remove all 2 rates this search shows" in listing
    admin_client.post(
        "/tax/rates/remove/", {"matching": "1", "q": "SST-53-D", "source": "sst", "confirm": "1"}
    )
    assert set(TaxRate.objects.values_list("jurisdiction__code", flat=True)) == {
        "SST-53-45-53",
        "SST-53-00-061",
        "SST-53-01-70000",
    }


@pytest.mark.django_db
def test_removing_needs_manage_permission(staff_client, setup_done):
    batch = ImportBatch.objects.create(source="sst", filename="WAB.zip")
    assert staff_client.post(f"/tax/imports/{batch.pk}/remove/").status_code in (302, 403)
    assert staff_client.post("/tax/rates/remove/", {"matching": "1"}).status_code in (302, 403)
    assert ImportBatch.objects.filter(pk=batch.pk).exists()


@pytest.mark.django_db
def test_removing_through_the_api(admin, tmp_path):
    import json

    from django.test import Client

    from trammel.accounts.models import ApiKey
    from trammel.core import access

    issued = ApiKey.issue(admin, "Tax", sorted(access.permissions_of(access.as_actor(admin))))
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    rates = import_bytes(RATES.encode(), "WAR2026Q4.csv", tmp_path)
    import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)

    listed = client.get("/api/v1/tax/imports").json()
    assert {i["filename"] for i in listed["items"]} == {"WAR2026Q4.csv", "WAB2026Q4.zip"}
    one = TaxRate.objects.get(jurisdiction__code="SST-53-00-061")
    removed = client.post(
        "/api/v1/tax/rates/remove",
        data=json.dumps({"ids": [str(one.pk)]}),
        content_type="application/json",
    ).json()
    assert removed == {"rates": 1, "boundaries": 0, "jurisdictions": 1}
    removed = client.delete(f"/api/v1/tax/imports/{rates.pk}").json()
    assert removed == {"rates": 5, "boundaries": 0, "jurisdictions": 5}
    assert not TaxRate.objects.exists()


@pytest.mark.django_db
def test_archive_without_sst_files_is_refused(setup_done, tmp_path):
    with pytest.raises(ValidationError, match="No SST"):
        import_bytes(zipped("readme.txt", "hello\n"), "other.zip", tmp_path)


@pytest.mark.django_db
def test_upload_runs_in_the_background(admin_client, django_capture_on_commit_callbacks):
    upload = SimpleUploadedFile("WAR2026Q4.zip", zipped("WAR2026Q4.csv", RATES))
    response = admin_client.post("/tax/import/", {"kind": "sst", "file": upload})
    assert response.status_code == 302
    batch = ImportBatch.objects.get()
    assert batch.status == "queued"
    tasks.run_pending()
    batch.refresh_from_db()
    assert batch.status == "done" and batch.rows == 6


@pytest.mark.django_db
def test_import_by_url(admin_client, monkeypatch):
    payload = zipped("WAR2026Q4.csv", RATES)

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    requested = []
    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port: (
            [(2, 1, 6, "", ("192.0.2.80", port))]
            if host == "test.invalid"
            else [(2, 1, 6, "", ("142.250.0.1", port))]
        ),
    )

    class FakeOpener:
        def open(self, req, timeout):
            requested.append(req.full_url)
            return FakeResponse(payload)

    monkeypatch.setattr(outbound.urllib.request, "build_opener", lambda *handlers: FakeOpener())
    url = "https://dor.wa.gov/sites/default/files/2026-08/WAR2026Q4.zip"
    admin_client.post("/tax/import/", {"kind": "sst", "url": url})
    tasks.run_pending()
    batch = ImportBatch.objects.get()
    assert requested == [url]
    assert (batch.status, batch.rows, batch.source_url, batch.filename) == (
        "done",
        6,
        url,
        "WAR2026Q4.zip",
    )


@pytest.mark.django_db
def test_a_redirect_into_the_local_network_is_refused(setup_done, monkeypatch):
    import urllib.error
    from email.message import Message

    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port, *rest: [
            (2, 1, 6, "", ("10.0.0.5" if host == "router.lan" else "142.250.0.1", port))
        ],
    )
    opened = []

    class Redirecting:
        def open(self, req, timeout):
            opened.append(req.full_url)
            headers = Message()
            headers["Location"] = "https://router.lan/admin"
            raise urllib.error.HTTPError(req.full_url, 302, "Found", headers, None)

    monkeypatch.setattr(outbound.urllib.request, "build_opener", lambda *handlers: Redirecting())
    with pytest.raises(ValidationError, match="private"):
        importers.download("https://example.com/rates.zip")
    assert opened == ["https://example.com/rates.zip"]  # the LAN address is never opened


@pytest.mark.django_db
def test_url_rules(setup_done, monkeypatch):
    with pytest.raises(ValidationError, match="https"):
        importers.check_url("http://dor.wa.gov/x.zip")
    monkeypatch.setattr(
        outbound.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("192.168.1.10", port))]
    )
    with pytest.raises(ValidationError, match="private"):
        importers.check_url("https://nas.local/rates.zip")
    from trammel.core.models import SystemSettings

    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    with pytest.raises(ValidationError, match="Outbound"):
        importers.download("https://dor.wa.gov/x.zip")


@pytest.mark.django_db
def test_failed_download_is_reported(admin_client, monkeypatch):
    monkeypatch.setattr(
        outbound.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("142.250.0.1", port))]
    )

    class Failing:
        def open(self, req, timeout):
            raise OSError("404 Not Found")

    monkeypatch.setattr(outbound.urllib.request, "build_opener", lambda *handlers: Failing())
    admin_client.post("/tax/import/", {"kind": "sst", "url": "https://dor.wa.gov/missing.zip"})
    tasks.run_pending()
    batch = ImportBatch.objects.get()
    assert batch.status == "failed" and "404" in batch.error
    assert "failed" in admin_client.get("/tax/").content.decode()


def test_a_download_connects_to_the_address_that_was_checked(monkeypatch):
    seen = {}

    def connect(address, timeout=None):
        seen["address"] = address
        return "socket"

    class Context:
        def wrap_socket(self, sock, server_hostname):
            seen["certificate_for"] = server_hostname
            return sock

    monkeypatch.setattr(outbound.socket, "create_connection", connect)
    connection = outbound.PinnedHTTPSConnection(
        "dor.wa.gov", 443, address="142.250.0.1", context=Context()
    )
    connection.connect()
    assert seen == {"address": ("142.250.0.1", 443), "certificate_for": "dor.wa.gov"}


@pytest.mark.django_db
def test_each_hop_is_pinned_to_its_own_checked_address(setup_done, monkeypatch):
    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port, *rest: [(2, 1, 6, "", ("142.250.0.9", port))],
    )
    pinned = []

    class Opener:
        def open(self, req, timeout):
            raise OSError("stop here")

    def build(*handlers):
        pinned.extend(h._address for h in handlers if isinstance(h, outbound.PinnedHTTPSHandler))
        return Opener()

    monkeypatch.setattr(outbound.urllib.request, "build_opener", build)
    with pytest.raises(OSError):
        importers._open("https://dor.wa.gov/rates.zip")
    assert pinned == ["142.250.0.9"]
