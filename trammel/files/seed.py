"""Starting document types. Runs after every migrate (ModuleManifest.seed).

Each default is created once, matched by its slug, and never changed again:
the admin may rename, reorder or switch it off.
"""

from __future__ import annotations

# (slug, name, extensions, show on travelers)
DEFAULT_TYPES = [
    ("sketch", "Customer sketch", "", True),
    ("drawing", "Production drawing", "pdf, dwg", True),
    ("cad", "CAD file", "dxf, step, stp, iges, igs, f3d, skp", True),
    ("model", "3D model", "stl, 3mf, obj", True),
    ("laser", "Laser file", "lbrn, lbrn2, svg, ai, eps", True),
    ("gcode", "G-code", "gcode, nc, ngc, tap, cnc", True),
    ("photo", "Photo", "jpg, jpeg, png, heic, webp", False),
    ("receipt", "Receipt or confirmation", "", False),
    # A customer's tax exemption certificate, and the shop's own resale certificate.
    ("exemption", "Tax exemption certificate", "", False),
    ("other", "Other", "", False),
]


def seed() -> None:
    from .models import DocumentType

    known = set(DocumentType.all_objects.values_list("slug", flat=True))
    for position, (slug, name, extensions, traveler) in enumerate(DEFAULT_TYPES, start=1):
        if slug in known or DocumentType.objects.filter(name__iexact=name).exists():
            continue
        DocumentType.objects.create(
            slug=slug,
            name=name,
            extensions=extensions,
            show_on_traveler=traveler,
            position=position * 10,
        )
