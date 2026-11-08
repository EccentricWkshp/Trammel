"""Background thumbnails for images and PDFs (FR-DOC-4)."""

from __future__ import annotations

from pathlib import Path

from trammel.core.tasks import task

THUMB_PX = 480


@task("files.thumbnail")
def make_thumbnail(payload: dict) -> None:
    from PIL import Image, ImageOps

    from .models import Blob
    from .services import blob_path, media_root

    blob = Blob.objects.filter(pk=payload["blob"]).first()
    if blob is None or blob.thumb_state == Blob.Thumb.READY:
        return
    source = blob_path(blob)
    relative = Path("thumbs") / blob.sha256[:2] / f"{blob.sha256}.webp"
    target = media_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if blob.kind == "pdf":
            import pypdfium2

            pdf = pypdfium2.PdfDocument(str(source))
            try:
                image = pdf[0].render(scale=1.5).to_pil()
            finally:
                pdf.close()
        else:
            image = Image.open(source)
            image = ImageOps.exif_transpose(image)
        image.thumbnail((THUMB_PX, THUMB_PX))
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(target, "WEBP", quality=80)
    except Exception:
        Blob.objects.filter(pk=blob.pk).update(thumb_state=Blob.Thumb.FAILED)
        raise
    Blob.objects.filter(pk=blob.pk).update(
        thumbnail=str(relative).replace("\\", "/"), thumb_state=Blob.Thumb.READY
    )
