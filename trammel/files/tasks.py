"""Background previews of stored files (FR-DOC-4): see previews.py."""

from __future__ import annotations

import logging
from pathlib import Path

from trammel.core.tasks import task

log = logging.getLogger(__name__)


@task("files.thumbnail")
def make_thumbnail(payload: dict) -> None:
    from . import previews
    from .models import Blob
    from .services import blob_path, media_root

    blob = Blob.objects.filter(pk=payload["blob"]).first()
    if blob is None or blob.thumb_state == Blob.Thumb.READY:
        return
    from trammel.core import limits

    try:
        # Read in a child with memory and time limits (SEC-02, 0.10.6): a file
        # that asks for too much fails alone, not the worker with it.
        made = limits.run(
            previews.render,
            blob.kind,
            blob_path(blob),
            memory_mb=previews.MEMORY_MB,
            seconds=previews.SECONDS,
        )
    except Exception:
        # A file its reader can't make sense of has no preview; trying again
        # won't change that.
        log.warning("No preview for %s (%s)", blob.sha256[:12], blob.kind, exc_info=True)
        Blob.objects.filter(pk=blob.pk).update(thumb_state=Blob.Thumb.FAILED)
        return
    if made is None:
        Blob.objects.filter(pk=blob.pk).update(thumb_state=Blob.Thumb.NONE)
        return
    data, ext = made
    relative = Path("thumbs") / blob.sha256[:2] / f"{blob.sha256}.{ext}"
    target = media_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    Blob.objects.filter(pk=blob.pk).update(
        thumbnail=str(relative).replace("\\", "/"), thumb_state=Blob.Thumb.READY
    )
