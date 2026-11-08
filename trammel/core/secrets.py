"""Encrypted credential storage (SPEC §9, FR-ADM-8, FR-NTF-1)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .models import Credential


def _fernet() -> Fernet:
    material = settings.CREDENTIAL_KEY or f"credential:{settings.SECRET_KEY}"
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())
    return Fernet(key)


def put(key: str, value: str) -> None:
    token = _fernet().encrypt(value.encode()).decode()
    Credential.objects.update_or_create(key=key, defaults={"ciphertext": token})


def get(key: str) -> str | None:
    """The secret, or None if unset or undecryptable (the key was rotated)."""
    row = Credential.objects.filter(key=key).first()
    if row is None:
        return None
    try:
        return _fernet().decrypt(row.ciphertext.encode()).decode()
    except InvalidToken:
        return None


def is_set(key: str) -> bool:
    return Credential.objects.filter(key=key).exists()


def clear(key: str) -> None:
    Credential.objects.filter(key=key).delete()
