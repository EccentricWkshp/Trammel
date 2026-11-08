"""Background import of large tax files (a state's boundary file is hundreds of MB)."""

from pathlib import Path

from trammel.core.tasks import task


@task("tax.import_sst")
def import_sst(payload):
    from django.core.exceptions import ValidationError

    from . import importers
    from .models import ImportBatch

    batch = ImportBatch.objects.filter(pk=payload["batch"]).first()
    if batch is None or batch.status not in ("queued", "running"):
        return
    ImportBatch.objects.filter(pk=batch.pk).update(status="running")
    path = Path(payload["path"]) if payload.get("path") else None
    try:
        if path is None:
            path = importers.download(payload["url"])
        importers.import_sst_path(
            path, filename=batch.filename, user=batch.imported_by, batch=batch
        )
    except (ValidationError, OSError, ValueError) as exc:
        message = " ".join(getattr(exc, "messages", [str(exc)]))
        ImportBatch.objects.filter(pk=batch.pk).update(status="failed", error=message[:2000])
        return  # a bad file is reported on the screen, not retried
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    ImportBatch.objects.filter(pk=batch.pk).update(status="done")
