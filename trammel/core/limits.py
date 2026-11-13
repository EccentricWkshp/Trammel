"""Work on untrusted input in a child process with memory and time limits.

File previews (PDFs, drawings, models a customer sent) and user-written
templates can ask for more memory or time than the server has, in ways a size
limit on the input doesn't bound. `run()` does such work in a forked child that
may use only so much memory (above what it starts with) and so much CPU time,
and is killed if it runs past its time, so a hostile input fails on its own
instead of taking the web server or the background worker down with it
(SEC-02, SEC-09, 0.10.6).

The child has no database: what it needs is worked out before it starts, and
any query it tried would fail rather than share the parent's connection. Where
`os.fork` or the `resource` module isn't available (Windows, for development),
or with `PROCESS_LIMITS` off, the work runs in this process as before.
"""

from __future__ import annotations

import logging
import os
import pickle
import select
import signal
import time
import warnings

from django.conf import settings

log = logging.getLogger("trammel.limits")


class Exceeded(Exception):
    """The work needed more memory or time than it's allowed."""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what  # "memory" or "time"


class NeedsDatabase(RuntimeError):
    """The work tried to use the database, which a limited child can't."""


def available() -> bool:
    if not getattr(settings, "PROCESS_LIMITS", True) or not hasattr(os, "fork"):
        return False
    try:
        import resource  # noqa: F401
    except ImportError:
        return False
    return True


def _cut_database() -> None:
    """In the child: drop the parent's connections without closing them (closing
    would end the parent's session) and refuse any new one."""
    from django.db import connections

    def refuse(*args, **kwargs):
        raise NeedsDatabase("No database in a limited process.")

    for alias in connections:
        conn = connections[alias]
        conn.connection = None
        conn.connect = refuse
        conn.ensure_connection = refuse


def _limit(memory_mb: int, seconds: int) -> None:
    import resource

    page = os.sysconf("SC_PAGE_SIZE")
    try:
        with open("/proc/self/statm") as statm:
            current = int(statm.read().split()[0]) * page
    except OSError:
        current = 0
    budget = current + memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (budget, budget))
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
    # A child killed for its limits leaves no core dump: it would fill the disk
    # and could hold what it was given (a customer's file, a document's data).
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run(func, *args, memory_mb: int = 512, seconds: int = 30, **kwargs):
    """func(*args, **kwargs) in a limited child, or here when limits aren't
    available. Raises Exceeded, NeedsDatabase, or what func raised."""
    if not available():
        return func(*args, **kwargs)
    read_end, write_end = os.pipe()
    with warnings.catch_warnings():
        # Forking a threaded web worker is safe here: the child only runs `func`
        # on what it was given, writes to the pipe and leaves with os._exit.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:  # pragma: no cover — runs in the child
        status = 1
        try:
            os.close(read_end)
            _cut_database()
            _limit(memory_mb, seconds)
            try:
                result = ("ok", func(*args, **kwargs))
            except MemoryError:
                result = ("memory", None)
            except NeedsDatabase as exc:
                result = ("database", str(exc))
            except BaseException as exc:  # noqa: BLE001 — sent back to be raised there
                result = ("error", exc)
            try:
                data = pickle.dumps(result)
            except Exception:  # noqa: BLE001
                data = pickle.dumps(("error", RuntimeError(repr(result[1])[:500])))
            with os.fdopen(write_end, "wb") as pipe:
                pipe.write(data)
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    return _collect(pid, read_end, seconds)


def _collect(pid: int, read_end: int, seconds: int):
    chunks = []
    deadline = time.monotonic() + seconds + 5
    timed_out = False
    with os.fdopen(read_end, "rb") as pipe:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([pipe], [], [], left)
            if not ready:
                continue
            chunk = os.read(pipe.fileno(), 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    if timed_out:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    if timed_out:
        raise Exceeded("time")
    if not chunks:
        # Killed without a word: over its CPU time (SIGXCPU or SIGKILL), or out
        # of memory before it could say so.
        signum = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
        raise Exceeded("time" if signum in (signal.SIGXCPU, signal.SIGKILL) else "memory")
    kind, value = pickle.loads(b"".join(chunks))  # noqa: S301 — from our own child
    if kind == "ok":
        return value
    if kind == "memory":
        raise Exceeded("memory")
    if kind == "database":
        raise NeedsDatabase(value)
    raise value
