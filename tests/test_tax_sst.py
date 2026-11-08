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

from trammel.core import tasks
from trammel.tax import importers, services
from trammel.tax.models import Boundary, ImportBatch, TaxRate

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
    import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q3.zip", tmp_path)
    import_bytes(zipped("WAB.csv", BOUNDARIES), "WAB2026Q4.zip", tmp_path)
    assert Boundary.objects.filter(source="sst").count() == 3
    import_bytes(RATES.encode(), "WAR2026Q3.csv", tmp_path)
    import_bytes(RATES.encode(), "WAR2026Q4.csv", tmp_path)
    assert TaxRate.objects.filter(source="sst").count() == 6


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
        importers.socket,
        "getaddrinfo",
        lambda host, port: (
            [(2, 1, 6, "", ("192.0.2.80", port))]
            if host == "test.invalid"
            else [(2, 1, 6, "", ("142.250.0.1", port))]
        ),
    )
    monkeypatch.setattr(
        importers.urllib.request,
        "urlopen",
        lambda req, timeout: requested.append(req.full_url) or FakeResponse(payload),
    )
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
def test_url_rules(setup_done, monkeypatch):
    with pytest.raises(ValidationError, match="https"):
        importers.check_url("http://dor.wa.gov/x.zip")
    monkeypatch.setattr(
        importers.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("192.168.1.10", port))]
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
        importers.socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", ("142.250.0.1", port))]
    )

    def boom(req, timeout):
        raise OSError("404 Not Found")

    monkeypatch.setattr(importers.urllib.request, "urlopen", boom)
    admin_client.post("/tax/import/", {"kind": "sst", "url": "https://dor.wa.gov/missing.zip"})
    tasks.run_pending()
    batch = ImportBatch.objects.get()
    assert batch.status == "failed" and "404" in batch.error
    assert "failed" in admin_client.get("/tax/").content.decode()
