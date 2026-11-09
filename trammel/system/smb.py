"""Backup copies on a network share over SMB: Windows shares and most NAS boxes.

The share is reached directly by the app, so nothing has to be mounted on the
host or into the container (which a folder target needs). Each operation opens
its own connection and closes it afterwards; nothing is kept between backups.

A copy is written under a temporary name and renamed when complete, so an
interrupted copy never looks like a backup to pruning or to a restore.
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024
TIMEOUT_SECONDS = 20
PARTIAL = ".part"


class SMBError(RuntimeError):
    """Anything that went wrong talking to the share, in words."""


@dataclass(frozen=True)
class Target:
    server: str
    share: str
    folder: str
    username: str
    password: str

    def path(self, name: str = "") -> str:
        parts = [f"\\\\{self.server}\\{self.share}"]
        parts += [p for p in self.folder.replace("/", "\\").split("\\") if p]
        if name:
            parts.append(name)
        return "\\".join(parts)


def _describe(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:300]


@contextlib.contextmanager
def _connected(target: Target):
    import smbclient

    cache: dict = {}
    options = {
        "username": target.username or None,
        "password": target.password or None,
        "connection_timeout": TIMEOUT_SECONDS,
        "connection_cache": cache,
    }
    try:
        yield smbclient, options
    except SMBError:
        raise
    except Exception as exc:  # noqa: BLE001 — smbprotocol, spnego and socket errors alike
        raise SMBError(_describe(exc)) from exc
    finally:
        with contextlib.suppress(Exception):
            smbclient.reset_connection_cache(connection_cache=cache)


def _ensure_folder(client, target: Target, options: dict) -> None:
    if target.folder.strip("\\/ "):
        client.makedirs(target.path(), exist_ok=True, **options)


def put_file(target: Target, source: Path) -> str:
    """Copy a local file into the share's folder; returns its path there."""
    final = target.path(source.name)
    partial = final + PARTIAL
    with _connected(target) as (client, options):
        _ensure_folder(client, target, options)
        with open(source, "rb") as src, client.open_file(partial, mode="wb", **options) as out:
            shutil.copyfileobj(src, out, CHUNK)
        client.replace(partial, final, **options)
    return final


def put_bytes(target: Target, name: str, data: bytes) -> str:
    path = target.path(name)
    with _connected(target) as (client, options):
        _ensure_folder(client, target, options)
        with client.open_file(path, mode="wb", **options) as out:
            out.write(data)
    return path


def list_names(target: Target, prefix: str = "") -> list[str]:
    """File names in the share's folder that start with `prefix`, sorted."""
    with _connected(target) as (client, options):
        names = client.listdir(target.path(), **options)
    return sorted(n for n in names if n.startswith(prefix))


def delete(target: Target, name: str) -> None:
    with _connected(target) as (client, options):
        client.remove(target.path(name), **options)
