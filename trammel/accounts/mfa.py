"""TOTP second factor (RFC 6238) and recovery codes (SPEC §9).

The secret is stored encrypted; a code is accepted once (replay protection) and
within one 30-second step either side, for clock drift — WSL2's clock can drift
after the host sleeps (§4.2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import InvalidToken
from django.utils import timezone

from trammel.core.secrets import _fernet

STEP = 30
DIGITS = 6
WINDOW = 1


def new_secret() -> str:
    return base64.b32encode(pysecrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // STEP)


def code_for(secret: str, at: float | None = None) -> str:
    return _code(secret, current_step(at))


def matching_step(secret: str, code: str, at: float | None = None) -> int | None:
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return None
    now = current_step(at)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}"
        f"&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"
    )


def encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def new_recovery_codes(n: int = 10) -> list[str]:
    return [f"{pysecrets.token_hex(3)}-{pysecrets.token_hex(3)}" for _ in range(n)]


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


# ---------------------------------------------------------------------------
# User-level operations
# ---------------------------------------------------------------------------


def enabled(user) -> bool:
    return bool(user.totp_secret and user.totp_confirmed_at)


def required(user) -> bool:
    from trammel.core.models import SystemSettings

    if user.is_admin and SystemSettings.load().require_mfa_for_admins:
        return True
    return user.roles.filter(require_mfa=True).exists()


def verify(user, code: str) -> bool:
    """A TOTP code, or an unused recovery code. Each is accepted once."""
    secret = decrypt(user.totp_secret) if user.totp_secret else None
    if secret:
        step = matching_step(secret, code)
        if step is not None and step > (user.totp_last_step or 0):
            user.totp_last_step = step
            user.save(update_fields=["totp_last_step"])
            return True
    digest = hash_code(code)
    if digest in (user.recovery_codes or []):
        user.recovery_codes = [c for c in user.recovery_codes if c != digest]
        user.save(update_fields=["recovery_codes"])
        return True
    return False


def enable(user, secret: str) -> list[str]:
    codes = new_recovery_codes()
    user.totp_secret = encrypt(secret)
    user.totp_confirmed_at = timezone.now()
    user.totp_last_step = current_step()
    user.recovery_codes = [hash_code(c) for c in codes]
    user.save(
        update_fields=["totp_secret", "totp_confirmed_at", "totp_last_step", "recovery_codes"]
    )
    return codes


def disable(user) -> None:
    user.totp_secret = ""
    user.totp_confirmed_at = None
    user.recovery_codes = []
    user.save(update_fields=["totp_secret", "totp_confirmed_at", "recovery_codes"])
