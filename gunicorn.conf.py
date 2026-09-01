"""Gunicorn configuration.

Only purpose right now: periodically hand freed heap memory back to the OS.

Under load, request processing (large querysets, JSON/template rendering,
boto3/Tigris upload buffers) grows each worker's glibc + pymalloc arenas.
Those arenas are mmap-backed, and freed objects stay parked in the arena
free lists — so a worker's RSS ratchets up on a spike and never falls back
on its own. `MALLOC_ARENA_MAX=2` (set in fly.toml) caps arena *count* but
does not return memory to the kernel.

`malloc_trim(0)` walks all arenas and madvise()s their free pages back to
the OS, so a spike actually comes back down. We call it every N requests
per worker to amortize its cost instead of paying it on every response.

Only meaningful on glibc (the Debian base image). On musl, or if the
symbol can't be resolved, the trim is skipped and the hook is a no-op.
"""

import ctypes
import logging

logger = logging.getLogger("gunicorn.error")

# Resolve malloc_trim once at import. Left as None when unavailable so the
# hook degrades to a no-op rather than crashing the worker.
_malloc_trim = None
try:
    _libc = ctypes.CDLL("libc.so.6")
    _malloc_trim = _libc.malloc_trim
    _malloc_trim.argtypes = [ctypes.c_size_t]
    _malloc_trim.restype = ctypes.c_int
except OSError, AttributeError:  # pragma: no cover - platform dependent
    logger.warning("malloc_trim unavailable; periodic heap trim disabled")

# Trim after this many requests per worker. Low enough that a memory spike is
# reclaimed soon after it happens; high enough that the trim's cost (walking
# arenas + madvise) is amortized across many requests. Tune against observed
# RSS behavior.
_TRIM_EVERY_REQUESTS = 50


def post_request(worker, req, environ, resp):
    """Return freed heap to the OS every _TRIM_EVERY_REQUESTS requests."""
    if _malloc_trim is None:
        return
    count = getattr(worker, "_requests_since_trim", 0) + 1
    if count >= _TRIM_EVERY_REQUESTS:
        _malloc_trim(0)
        count = 0
    worker._requests_since_trim = count
