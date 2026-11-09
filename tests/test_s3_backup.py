"""The S3-compatible off-site backup copy (FR-BAK-1)."""

import datetime as dt
import http.server
import threading
import urllib.parse
from xml.sax.saxutils import escape

import pytest

from tests.test_system import fake_pg_dump  # noqa: F401 — fixture
from trammel.core import secrets
from trammel.core.models import SystemSettings
from trammel.system import backup, s3
from trammel.system.models import Backup, BackupSettings

AWS_EXAMPLE = s3.Target(
    endpoint="https://examplebucket.s3.amazonaws.com",
    region="us-east-1",
    bucket="",
    access_key="AKIAIOSFODNN7EXAMPLE",
    secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
)
WHEN = dt.datetime(2013, 5, 24, tzinfo=dt.UTC)


def _signature(headers):
    return headers["authorization"].rsplit("Signature=", 1)[1]


def test_signing_key_matches_aws_example():
    key = s3.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215", "us-east-1", "iam")
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_get_object_signature_matches_aws_example():
    # "Example: GET Object" in the S3 Signature Version 4 documentation.
    url, headers = s3.sign(AWS_EXAMPLE, "GET", "test.txt", headers={"range": "bytes=0-9"}, now=WHEN)
    assert url == "https://examplebucket.s3.amazonaws.com/test.txt"
    assert _signature(headers) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"


def test_list_objects_signature_matches_aws_example():
    # "Example: GET Bucket (List Objects)".
    url, headers = s3.sign(AWS_EXAMPLE, "GET", "", {"max-keys": "2", "prefix": "J"}, now=WHEN)
    assert url.endswith("/?max-keys=2&prefix=J")
    assert _signature(headers) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


def test_only_http_endpoints():
    target = s3.Target("file:///etc", "us-east-1", "b", "k", "s")
    with pytest.raises(s3.S3Error, match="https"):
        s3.put_bytes(target, "x", b"x")


class FakeS3(http.server.BaseHTTPRequestHandler):
    """Just enough S3: PUT, GET ?list-type=2, DELETE, keyed by path. Checks a signature is sent."""

    objects: dict[str, bytes] = {}
    fail = False

    def _key(self):
        path = urllib.parse.urlsplit(self.path).path
        return urllib.parse.unquote(path.split("/", 2)[2]) if path.count("/") >= 2 else ""

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check(self):
        if self.fail or "AWS4-HMAC-SHA256" not in self.headers.get("Authorization", ""):
            self._reply(
                403, b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
            )
            return False
        return True

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self._check():
            self.objects[self._key()] = body
            self._reply(200)

    def do_GET(self):
        if self._check():
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            prefix = query.get("prefix", [""])[0]
            keys = "".join(
                f"<Contents><Key>{escape(k)}</Key></Contents>"
                for k in sorted(self.objects)
                if k.startswith(prefix)
            )
            body = (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>false</IsTruncated>{keys}</ListBucketResult>"
            ).encode()
            self._reply(200, body)

    def do_DELETE(self):
        if self._check():
            self.objects.pop(self._key(), None)
            self._reply(204)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_s3():
    FakeS3.objects, FakeS3.fail = {}, False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def s3_settings(setup_done, fake_s3):
    cfg = BackupSettings.load()
    cfg.s3_enabled = True
    cfg.s3_endpoint = fake_s3
    cfg.s3_bucket = "shop-backups"
    cfg.s3_prefix = "trammel"
    cfg.s3_access_key = "AKIDEXAMPLE"
    cfg.keep = 2
    cfg.save()
    secrets.put(BackupSettings.S3_SECRET, "secret")
    return cfg


@pytest.mark.django_db
def test_backup_is_copied_to_s3_and_pruned(s3_settings, fake_pg_dump, monkeypatch):  # noqa: F811
    stamps = ["20261101-010000", "20261102-010000", "20261103-010000"]
    record = None
    for stamp in stamps:
        when = dt.datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=dt.UTC)
        monkeypatch.setattr(backup.timezone, "now", lambda when=when: when)
        record = backup.create("manual")
        assert record.status == "ok" and record.s3 and not record.copy_error
    assert sorted(FakeS3.objects) == [
        "trammel/trammel-20261102-010000-manual.tar.gz",
        "trammel/trammel-20261103-010000-manual.tar.gz",
    ]
    stored = FakeS3.objects[f"trammel/{record.filename}"]
    assert stored == (backup.backup_dir() / record.filename).read_bytes()


@pytest.mark.django_db
def test_s3_failure_keeps_the_backup_and_says_why(s3_settings, fake_pg_dump):  # noqa: F811
    FakeS3.fail = True
    record = backup.create("manual")
    assert record.status == "ok" and not record.s3
    assert "Access Denied" in record.copy_error


@pytest.mark.django_db
def test_s3_is_skipped_when_outbound_traffic_is_off(s3_settings, fake_pg_dump):  # noqa: F811
    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    record = backup.create("manual")
    assert not record.s3 and "outbound" in record.copy_error and not FakeS3.objects


@pytest.mark.django_db
def test_connection_test_and_settings_screen(admin_client, s3_settings):
    assert backup.test_s3().startswith("OK") and not FakeS3.objects  # test file cleaned up
    response = admin_client.post("/settings/maintenance/backup/test-s3/", follow=True)
    assert "Trammel can write" in response.content.decode()
    page = admin_client.get("/settings/maintenance/").content.decode()
    assert "Save and test S3" in page and 'value="secret"' not in page


@pytest.mark.django_db
def test_settings_keep_the_secret_unless_replaced(admin_client, s3_settings):
    data = {
        "interval_hours": "24",
        "keep": "2",
        "warn_after_days": "7",
        "s3_enabled": "on",
        "s3_endpoint": s3_settings.s3_endpoint,
        "s3_region": "us-east-1",
        "s3_bucket": "shop-backups",
        "s3_prefix": "trammel",
        "s3_access_key": "AKIDEXAMPLE",
        "s3_secret_key": "",
    }
    admin_client.post("/settings/maintenance/", data)
    assert secrets.get(BackupSettings.S3_SECRET) == "secret"
    admin_client.post("/settings/maintenance/", {**data, "s3_secret_key": "new-secret"})
    assert secrets.get(BackupSettings.S3_SECRET) == "new-secret"
    response = admin_client.post(
        "/settings/maintenance/", {**data, "s3_clear_secret": "on", "s3_access_key": ""}
    )
    assert response.status_code == 200 and "Needed while this copy" in response.content.decode()
    assert Backup.objects.count() == 0


@pytest.mark.django_db
def test_offbox_folder_is_pruned_but_other_files_are_left(
    setup_done,
    fake_pg_dump,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    folder = tmp_path / "nas"
    folder.mkdir()
    (folder / "family-photos.zip").write_bytes(b"keep me")
    cfg = BackupSettings.load()
    cfg.offbox_enabled, cfg.offbox_path, cfg.keep = True, str(folder), 1
    cfg.save()
    for day in (1, 2):
        when = dt.datetime(2026, 11, day, tzinfo=dt.UTC)
        monkeypatch.setattr(backup.timezone, "now", lambda when=when: when)
        backup.create("manual")
    assert sorted(p.name for p in folder.iterdir()) == [
        "family-photos.zip",
        "trammel-20261102-000000-manual.tar.gz",
    ]
