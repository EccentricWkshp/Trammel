"""
Full export (FR-BAK-5): the shop's data, readable without Trammel.

A `.zip` holding:

* `README.md` — what each file is, and the format;
* `data/<app>.<model>.ndjson` — one JSON object per line per record, every field,
  foreign keys as the related record's id, deleted records included with their
  `deleted_at` set;
* `files/<document id>/r<revision>-<original name>` — every uploaded file, as
  uploaded.

Never exported: password hashes, stored credentials, API key hashes, sessions.
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from trammel.core import audit

from .models import Export

EXCLUDED_MODELS = {
    "core.credential",
    "accounts.loginattempt",
    "core.backgroundtask",
    "core.periodicrun",
}
EXCLUDED_FIELDS = {
    "password",
    "secret_hash",
    "ciphertext",
    "totp_secret",
    "recovery_codes",
    "token_hash",
    "code_hash",
}

README = """\
# Trammel data export

Exported {when} from {business}, Trammel {version}.

## Layout

- `data/` — one file per kind of record, named `<app>.<model>.ndjson`.
  Each line is one JSON object: every field of the record. A field that refers
  to another record holds that record's `id`. Money is a decimal string;
  times are ISO 8601 in UTC; dates are `YYYY-MM-DD`. Binary content (the
  business logo, `core.businessprofile.logo`) is base64, with its type in
  `logo_type`.
  Records in the trash are included, with `deleted_at` set.
- `files/` — every uploaded file, byte-for-byte, in `files/<document id>/`,
  named `r<revision>-<original file name>`. `data/files.documentrevision.ndjson`
  maps revisions to documents; `data/files.attachment.ndjson` links documents to
  the records they belong to (`content_type` names the kind of record).

Passwords, stored credentials and API keys are not exported.

## Files in this export

{index}
"""


def _value(value):
    # Binary content (the business logo) as base64, since JSON has no bytes.
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value


def _rows(model):
    fields = [f for f in model._meta.concrete_fields if f.name not in EXCLUDED_FIELDS]
    manager = getattr(model, "all_objects", model._base_manager)
    for obj in manager.all().iterator():
        yield {
            f.attname if f.is_relation else f.name: _value(getattr(obj, f.attname)) for f in fields
        }


def build() -> Export:
    from trammel.core.models import BusinessProfile
    from trammel.files.models import DocumentRevision

    record = Export.objects.create()
    out_dir = Path(settings.BACKUP_DIR) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"trammel-export-{timezone.now():%Y%m%d-%H%M%S}.zip"
    path = out_dir / name
    index = []
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for model in apps.get_models():
                label = model._meta.label_lower
                ours = model.__module__.startswith("trammel.")
                wanted = label == "contenttypes.contenttype" or (
                    ours and label not in EXCLUDED_MODELS
                )
                if not wanted:
                    continue
                lines = [json.dumps(row, cls=DjangoJSONEncoder) for row in _rows(model)]
                zf.writestr(f"data/{label}.ndjson", "\n".join(lines) + ("\n" if lines else ""))
                index.append(f"- `data/{label}.ndjson` — {len(lines)} record(s)")
            media = Path(settings.MEDIA_ROOT)
            for rev in DocumentRevision.objects.select_related("blob").iterator():
                src = media / rev.blob.path
                if src.exists():
                    safe = rev.original_name.replace("/", "_").replace("\\", "_")
                    zf.write(src, f"files/{rev.document_id}/r{rev.number}-{safe}")
            business = BusinessProfile.load()
            zf.writestr(
                "README.md",
                README.format(
                    when=timezone.now().isoformat(),
                    business=business.name,
                    version=settings.APP_VERSION,
                    index="\n".join(sorted(index)),
                ),
            )
    except OSError as exc:
        Export.objects.filter(pk=record.pk).update(status="failed", error=str(exc))
        record.refresh_from_db()
        return record
    Export.objects.filter(pk=record.pk).update(status="ok", filename=name, size=path.stat().st_size)
    record.refresh_from_db()
    audit.record(
        "export.full",
        entity_type="system.export",
        entity_id=str(record.pk),
        summary=f"Built a full data export ({name})",
    )
    return record
