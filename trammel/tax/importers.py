"""
Tax rate sources that read files (FR-TAX-6). Invoices never make network calls;
an admin may fetch a state's published file by URL to import it.

Generic CSV
-----------
A header row, then one row per postal code or range::

    postal_code,jurisdiction_code,jurisdiction_name,rate,effective_from,effective_to
    98101,1726,Seattle,10.35,2025-04-01,
    98402-98409,2717,Tacoma,10.30,2025-04-01,

`postal_code` is a ZIP, a ZIP+4, or a range "low-high" of either. `rate` is a
percentage. Dates are YYYY-MM-DD; `effective_to` may be blank.

Streamlined Sales Tax (SST) files
---------------------------------
SST member states publish free rate and boundary files each quarter, usually as
ZIP archives (Washington: `WAR2026Q4.zip` rates, `WAB2026Q4.zip` boundaries).
Upload either — or give its URL — as a `.zip` or the `.csv` inside; the kind of
each file is detected from its first line. Both are comma-separated, no header.

*Rate file*: state FIPS, jurisdiction type, jurisdiction code, general rate
intrastate, general rate interstate, food/drug intrastate, food/drug interstate,
begin date, end date. Rates are fractions (0.065 = 6.5 %); dates YYYYMMDD; an
end year of 9999 means no end date. Types 45, 00 and 01 are the state, a county
and a city (FIPS place); every other type is a district, keyed by its code alone
(Washington lists location codes such as `L2717` as type 63 here but type 45 in
the boundary file).

*Boundary file*: 90 columns. Record types "Z" (5-digit ZIP range) and "4"
(ZIP+4 range) are read; "A" (street address ranges) are skipped — matching
stops at ZIP+4. The columns used are in `_B`. The jurisdictions listed on a
record are additive: the state plus whichever county, city and districts it
names (Washington: 6.5 % state + 4.0 % Tacoma = 10.5 %).

Importing a state's new boundary file replaces that state's previous SST
boundaries; a new rate file replaces its previous SST rates. Rates on issued
invoices are snapshots and never change.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import http.client
import io
import ipaddress
import socket
import ssl
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from trammel.core import audit

from .models import Boundary, ImportBatch, Jurisdiction, Source, TaxRate
from .services import normalize_postal

#: Largest file an import will download (WA's boundary ZIP is ~20 MB).
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
#: Boundary records that ended longer ago than this are not imported.
BOUNDARY_HISTORY_DAYS = 2 * 365
_BULK = 5000


def _date(value: str, fmt: str = "%Y-%m-%d") -> dt.date | None:
    value = (value or "").strip()
    if not value:
        return None
    return dt.datetime.strptime(value, fmt).date()


def _sst_date(value: str) -> dt.date | None:
    d = _date(value, "%Y%m%d")
    return None if d is None or d.year >= 2999 else d


def _zip_range(value: str) -> tuple[str, str]:
    value = value.strip()
    if "-" in value and len(value.replace("-", "")) != 9:
        low, high = (part.strip() for part in value.split("-", 1))
    else:
        low = high = value
    low9, high9 = normalize_postal(low), normalize_postal(high)
    if low9 is None or high9 is None:
        raise ValueError(f"not a ZIP code: {value!r}")
    if len(low.replace("-", "")) == 5:
        high9 = high9[:5] + "9999"
    return low9, high9


def _lines(data: bytes | str | Iterable[str]) -> Iterable[str]:
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    if isinstance(data, str):
        return data.splitlines()
    return data


def _record(batch: ImportBatch, errors: list[str]) -> None:
    audit.record(
        "tax.import",
        batch,
        summary=f"Imported {batch.rows} {batch.kind} row(s) from {batch.filename}"
        + (f", skipped {batch.skipped}" if batch.skipped else ""),
        after={"errors": errors[:20]} if errors else None,
    )


# ---------------------------------------------------------------------------
# Generic CSV
# ---------------------------------------------------------------------------


@transaction.atomic
def import_generic_csv(data: bytes, *, filename: str, user=None) -> ImportBatch:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    required = {"postal_code", "jurisdiction_code", "rate", "effective_from"}
    if not reader.fieldnames or not required <= {f.strip() for f in reader.fieldnames}:
        raise ValidationError(f"The CSV needs a header row with: {', '.join(sorted(required))}.")
    batch = ImportBatch.objects.create(
        source=Source.CSV, kind="combined", filename=filename, imported_by=user
    )
    errors = []
    for line, row in enumerate(reader, start=2):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        try:
            low, high = _zip_range(row["postal_code"])
            rate = Decimal(row["rate"])
            start = _date(row["effective_from"])
            end = _date(row.get("effective_to", ""))
        except (ValueError, InvalidOperation) as exc:
            errors.append(f"Line {line}: {exc}")
            batch.skipped += 1
            continue
        code = row["jurisdiction_code"]
        jurisdiction, _ = Jurisdiction.objects.get_or_create(
            code=code,
            defaults={
                "name": row.get("jurisdiction_name") or code,
                "source": Source.CSV,
                "level": Jurisdiction.Level.COMBINED,
            },
        )
        if not TaxRate.objects.filter(
            jurisdiction=jurisdiction, kind=TaxRate.Kind.COLLECTED, rate=rate, effective_from=start
        ).exists():
            TaxRate.objects.create(
                jurisdiction=jurisdiction,
                rate=rate,
                effective_from=start,
                effective_to=end,
                source=Source.CSV,
                batch=batch,
            )
        Boundary.objects.create(
            zip_low=low,
            zip_high=high,
            jurisdiction_codes=[code],
            location_code=code,
            effective_from=start or dt.date(2000, 1, 1),
            effective_to=end,
            source=Source.CSV,
            batch=batch,
        )
        batch.rows += 1
    if errors and not batch.rows:
        raise ValidationError(errors[:10])
    batch.save(update_fields=["rows", "skipped"])
    _record(batch, errors)
    return batch


# ---------------------------------------------------------------------------
# SST rate and boundary files
# ---------------------------------------------------------------------------

_LEVELS = {
    "45": Jurisdiction.Level.STATE,
    "00": Jurisdiction.Level.COUNTY,
    "01": Jurisdiction.Level.CITY,
}


def sst_code(state: str, jtype: str, code: str) -> str:
    """The jurisdiction key shared by rate and boundary files."""
    kind = jtype if jtype in _LEVELS else "D"
    return f"SST-{state}-{kind}-{code}"


def detect(first_line: str) -> str | None:
    """'rates', 'boundaries', or None for a file that is neither."""
    fields = first_line.strip().split(",")
    if fields and fields[0] in ("Z", "4", "A") and len(fields) > _B.PLACE:
        return "boundaries"
    if len(fields) == 9 and fields[0].isdigit() and len(fields[1]) == 2:
        return "rates"
    return None


def import_sst_rates(
    data, *, filename: str, user=None, batch: ImportBatch | None = None
) -> ImportBatch:
    batch = batch or ImportBatch.objects.create(
        source=Source.SST, kind="rates", filename=filename, imported_by=user
    )
    rows, states = [], set()
    for row in csv.reader(_lines(data)):
        if len(row) < 9:
            batch.skipped += 1
            continue
        try:
            rate = Decimal(row[3].strip()) * 100
            start, end = _sst_date(row[7]), _sst_date(row[8])
        except InvalidOperation, ValueError:
            batch.skipped += 1
            continue
        if start is None:
            batch.skipped += 1
            continue
        state, jtype, code = row[0].strip(), row[1].strip(), row[2].strip()
        states.add(state)
        rows.append((sst_code(state, jtype, code), jtype, code, state, rate, start, end))
    with transaction.atomic():
        # A new rate file is complete: it replaces the state's previous SST rates.
        for state in states:
            TaxRate.objects.filter(
                source=Source.SST, jurisdiction__code__startswith=f"SST-{state}-"
            ).delete()
        existing = dict(
            Jurisdiction.objects.filter(code__in={r[0] for r in rows}).values_list("code", "pk")
        )
        new = []
        for key, jtype, code, state, *_ in rows:
            if key not in existing:
                level = _LEVELS.get(jtype, Jurisdiction.Level.DISTRICT)
                name = {
                    "45": f"State ({state})",
                    "00": f"County {code}",
                    "01": f"City/place {code}",
                }.get(jtype, f"District {code}")
                j = Jurisdiction(code=key, name=name, level=level, source=Source.SST)
                existing[key] = j.pk
                new.append(j)
        Jurisdiction.objects.bulk_create(new, batch_size=_BULK)
        TaxRate.objects.bulk_create(
            [
                TaxRate(
                    jurisdiction_id=existing[key],
                    kind=TaxRate.Kind.COLLECTED,
                    rate=rate,
                    effective_from=start,
                    effective_to=end,
                    source=Source.SST,
                    batch=batch,
                )
                for key, _t, _c, _s, rate, start, end in rows
            ],
            batch_size=_BULK,
        )
        batch.rows += len(rows)
        batch.save(update_fields=["rows", "skipped"])
    _record(batch, [])
    return batch


class _B:
    """Zero-based column positions in an SST boundary record (checked against WA's file)."""

    TYPE, BEGIN, END = 0, 1, 2
    ZIP_LOW, EXT_LOW, ZIP_HIGH, EXT_HIGH = 17, 18, 19, 20
    SER, STATE = 21, 22
    COUNTY, PLACE = 24, 25
    FIRST_DISTRICT = 29  # then (source, code, type) triples


def _location_code(codes: list[str], ser: str) -> str:
    """The state's own location code, for grouping reports (FR-TAX-7).

    Washington writes it as a district code (`L2717`) or in the composite SER
    column (`02717`); both mean location code 2717.
    """
    for code in codes:
        if "-D-" in code:
            value = code.rsplit("-", 1)[1]
            return value[1:] if value[:1] == "L" and value[1:].isdigit() else value
    return ser.strip().lstrip("0") or ser.strip()


def import_sst_boundaries(
    data, *, filename: str, user=None, batch: ImportBatch | None = None
) -> ImportBatch:
    batch = batch or ImportBatch.objects.create(
        source=Source.SST, kind="boundaries", filename=filename, imported_by=user
    )
    oldest = timezone.localdate() - dt.timedelta(days=BOUNDARY_HISTORY_DAYS)
    with transaction.atomic():
        replaced: set[str] = set()
        pending: list[Boundary] = []
        for row in csv.reader(_lines(data)):
            kind = row[0].strip().upper() if row else ""
            if kind not in ("Z", "4") or len(row) <= _B.PLACE:
                batch.skipped += 1
                continue
            try:
                start = _sst_date(row[_B.BEGIN]) or dt.date(2000, 1, 1)
                end = _sst_date(row[_B.END])
            except ValueError:
                batch.skipped += 1
                continue
            if end is not None and end < oldest:
                batch.skipped += 1
                continue
            state = row[_B.STATE].strip()
            if state not in replaced:
                # A new boundary file is complete: drop the state's previous SST boundaries.
                Boundary.objects.filter(
                    source=Source.SST, jurisdiction_codes__0=sst_code(state, "45", state)
                ).exclude(batch=batch).delete()
                replaced.add(state)
            if kind == "Z":
                low = row[_B.ZIP_LOW].strip().zfill(5) + "0000"
                high = row[_B.ZIP_HIGH].strip().zfill(5) + "9999"
            else:
                low = row[_B.ZIP_LOW].strip().zfill(5) + row[_B.EXT_LOW].strip().zfill(4)
                high = row[_B.ZIP_HIGH].strip().zfill(5) + row[_B.EXT_HIGH].strip().zfill(4)
            codes = [sst_code(state, "45", state)]
            if county := row[_B.COUNTY].strip():
                codes.append(sst_code(state, "00", county))
            if place := row[_B.PLACE].strip():
                codes.append(sst_code(state, "01", place))
            i = _B.FIRST_DISTRICT
            while i + 1 < len(row):
                if code := row[i + 1].strip():
                    codes.append(sst_code(state, "D", code))
                i += 3
            location = _location_code(codes, row[_B.SER])
            pending.append(
                Boundary(
                    zip_low=low,
                    zip_high=high,
                    jurisdiction_codes=codes,
                    location_code=location,
                    effective_from=start,
                    effective_to=end,
                    source=Source.SST,
                    batch=batch,
                )
            )
            if len(pending) >= _BULK:
                Boundary.objects.bulk_create(pending)
                batch.rows += len(pending)
                pending = []
        Boundary.objects.bulk_create(pending)
        batch.rows += len(pending)
        batch.save(update_fields=["rows", "skipped"])
    _record(batch, [])
    return batch


# ---------------------------------------------------------------------------
# Files, archives and URLs
# ---------------------------------------------------------------------------


def _members(path: Path) -> Iterator[tuple[str, Iterable[str]]]:
    """(name, lines) for a .csv/.txt file, or for each one inside a .zip."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith((".csv", ".txt")):
                    continue
                with zf.open(info) as fh:
                    yield info.filename, io.TextIOWrapper(fh, encoding="latin-1", newline="")
    else:
        with path.open(encoding="latin-1", newline="") as fh:
            yield path.name, fh


def import_sst_path(
    path: Path, *, filename: str, user=None, batch: ImportBatch | None = None
) -> ImportBatch:
    """Import every SST rate or boundary file in `path` (a .zip or a .csv)."""
    batch = batch or ImportBatch.objects.create(
        source=Source.SST, kind="sst", filename=filename, imported_by=user
    )
    kinds = []
    for name, lines in _members(path):
        it = iter(lines)
        first = next(it, "")
        kind = detect(first)
        if kind is None:
            continue
        chained = _chain(first, it)
        if kind == "rates":
            import_sst_rates(chained, filename=name, user=user, batch=batch)
        else:
            import_sst_boundaries(chained, filename=name, user=user, batch=batch)
        kinds.append(kind)
    if not kinds:
        raise ValidationError("No SST rate or boundary file was found in it.")
    batch.kind = " + ".join(dict.fromkeys(kinds))
    batch.save(update_fields=["kind"])
    return batch


def _chain(first: str, rest: Iterator[str]) -> Iterator[str]:
    yield first
    yield from rest


def check_url(url: str) -> str:
    """Refuse anything but https to a public address (no reaching into the LAN).
    Returns the address checked, which the download then connects to, so the
    name can't be pointed somewhere else in between (DNS rebinding)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValidationError("Give an https:// address.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise ValidationError(f"Cannot resolve {parsed.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise ValidationError("That address is on a private network.")
    return infos[0][4][0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to one checked address, with the certificate still checked against
    the host name."""

    def __init__(self, host, *args, address: str, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, address: str):
        super().__init__(context=ssl.create_default_context())
        self._address = address

    def https_open(self, req):
        return self.do_open(
            functools.partial(_PinnedHTTPSConnection, address=self._address),
            req,
            context=self._context,
        )


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects come back as errors, so each new address is checked first."""

    def redirect_request(self, *args, **kwargs):
        return None


MAX_REDIRECTS = 5


def _open(url: str):
    """Open `url`, following redirects only to addresses check_url accepts."""
    import urllib.error
    from urllib.parse import urljoin

    for _ in range(MAX_REDIRECTS + 1):
        address = check_url(url)
        opener = urllib.request.build_opener(_NoRedirects, _PinnedHTTPSHandler(address))
        request = urllib.request.Request(  # noqa: S310 — https checked above
            url, headers={"User-Agent": f"Trammel/{settings.APP_VERSION} (tax import)"}
        )
        try:
            return opener.open(request, timeout=60)
        except urllib.error.HTTPError as exc:
            location = (
                exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            )
            if not location:
                raise
            url = urljoin(url, location)
    raise ValidationError("The address redirects too many times.")


def download(url: str) -> Path:
    """Fetch a published tax file to a temporary path. Honors the outbound switch."""
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise ValidationError("Outbound traffic is switched off; upload the file instead.")
    check_url(url)
    target_dir = Path(settings.MEDIA_ROOT) / "tmp"
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target_dir, suffix=Path(urlparse(url).path).suffix)
    path = Path(name)
    total = 0
    try:
        # The file is entered first, so it is closed even when the download fails.
        with (
            open(fd, "wb") as out,
            _open(url) as response,
        ):
            for chunk in iter(lambda: response.read(1 << 20), b""):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValidationError("The file is larger than the import limit.")
                out.write(chunk)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise ValidationError(f"Could not download the file: {exc}") from exc
    except ValidationError:
        path.unlink(missing_ok=True)
        raise
    return path
