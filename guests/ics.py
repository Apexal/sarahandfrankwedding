"""Hand-rendered iCalendar file for the wedding day (PLAN.md section 10).

One all-day `VEVENT` carrying the couple's names, the city, and the site URL. Rendered by
hand rather than with a library: RFC 5545 needs CRLF line endings, backslash-escaped text,
and lines folded at 75 octets, and that is all there is to it.
"""

from datetime import UTC, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

from guests.email_contexts import wedding_date_display
from pages.models import WeddingSettings

CRLF = "\r\n"
MAX_LINE_OCTETS = 75
CONTENT_TYPE = "text/calendar; charset=utf-8"


def escape_text(value: str) -> str:
    """Escape a TEXT property value per RFC 5545 section 3.3.11."""
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold a content line at 75 octets; continuation lines start with a space.

    Splits on character boundaries so a multi-byte character is never cut in half.
    """
    parts: list[str] = []
    current = ""
    used = 0
    for char in line:
        size = len(char.encode("utf-8"))
        if used + size > MAX_LINE_OCTETS:
            parts.append(current)
            current, used = " ", 1
        current += char
        used += size
    parts.append(current)
    return CRLF.join(parts)


def ics_filename(couple_names: str) -> str:
    """ "Sarah & Frank" → "sarah-and-frank-wedding.ics"."""
    slug = slugify(couple_names.replace("&", " and "))
    return f"{slug}-wedding.ics" if slug else "wedding.ics"


def event_summary(couple_names: str) -> str:
    return f"{couple_names}'s wedding"


def render_ics(wedding: WeddingSettings, *, site_url: str | None = None, now=None) -> str:
    """The .ics body. `site_url` and `now` are overridable for tests; both default sensibly."""
    site_url = (site_url or settings.APP_BASE_URL).rstrip("/")
    host = urlsplit(site_url).hostname or "wedding"
    stamp = (now or timezone.now()).astimezone(UTC)
    start = wedding.wedding_date
    end = start + timedelta(days=1)  # DTEND is exclusive for all-day events

    when = wedding_date_display(wedding)
    where = f" in {wedding.wedding_city}" if wedding.wedding_city else ""
    description = f"{wedding.couple_names} are getting married on {when}{where}. Details: {site_url}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{escape_text(wedding.couple_names)}//Wedding//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        # Stable across re-downloads and date changes so a calendar updates the one event.
        f"UID:wedding@{host}",
        f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end:%Y%m%d}",
        f"SUMMARY:{escape_text(event_summary(wedding.couple_names))}",
    ]
    if wedding.wedding_city:
        lines.append(f"LOCATION:{escape_text(wedding.wedding_city)}")
    lines += [
        f"URL:{site_url}",
        f"DESCRIPTION:{escape_text(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return CRLF.join(fold(line) for line in lines) + CRLF
