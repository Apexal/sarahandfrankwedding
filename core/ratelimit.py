"""Cache-backed per-IP rate limiting for the public POST endpoints.

Counters are fixed windows in the default cache, which is the DatabaseCache in
production and therefore shared by every gunicorn worker. `REMOTE_ADDR` is already the
real client IP behind Fly thanks to `TrustedClientIPMiddleware`. The decorator is a
no-op under pytest (`settings.TESTING`) so tests assert on behaviour, not on counts;
`is_rate_limited()` can be exercised directly.
"""

import functools
import re
import time

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render

_RATE = re.compile(r"^(?P<count>\d+)/(?P<number>\d*)(?P<unit>[smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_rate(rate: str) -> tuple[int, int]:
    """'10/15m' → (10, 900); '20/h' → (20, 3600)."""
    match = _RATE.match(rate)
    if not match:
        raise ValueError(f"Bad rate {rate!r}; use e.g. '10/15m' or '20/h'.")
    number = int(match["number"] or 1)
    return int(match["count"]), number * _UNIT_SECONDS[match["unit"]]


def is_rate_limited(request, *, scope: str, count: int, seconds: int) -> bool:
    ip = request.META.get("REMOTE_ADDR", "unknown")
    window = int(time.time() // seconds)
    key = f"ratelimit:{scope}:{ip}:{window}"
    if cache.add(key, 1, timeout=seconds):
        return count < 1
    try:
        current = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, timeout=seconds)
        return count < 1
    return current > count


def ratelimit(rate: str, *, methods: tuple[str, ...] = ("POST",), scope: str | None = None):
    """Decorate a view: over `rate` requests per window from one IP renders 429.html.

    Only the listed methods count, so a GET of a form never burns the budget.
    """
    count, seconds = parse_rate(rate)

    def decorator(view):
        view_scope = scope or f"{view.__module__}.{view.__name__}"

        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if (
                request.method in methods
                and not getattr(settings, "TESTING", False)
                and is_rate_limited(request, scope=view_scope, count=count, seconds=seconds)
            ):
                return render(request, "429.html", status=429)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
