"""Field types shared by the module APIs."""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import WithJsonSchema


def choices(enum: type[Enum]):
    """A model's choices enum, documented inline rather than as a named schema.

    Ninja merges every schema's definitions by bare class name, and several
    models have an enum called `Kind` (customers, products, machines, invoices).
    As named definitions they overwrite one another and the docs list the wrong
    values; inline, each field shows its own.
    """
    return Annotated[enum, WithJsonSchema({"type": "string", "enum": [m.value for m in enum]})]
