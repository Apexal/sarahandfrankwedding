"""Timezone utility functions for consistent timezone display across the app."""

import re
from datetime import date
from zoneinfo import ZoneInfo

from django.utils import timezone

# Map standard/daylight abbreviations to simplified forms for US timezones
_TZ_ABBR_SIMPLIFY = {
    "EST": "ET",
    "EDT": "ET",
    "CST": "CT",
    "CDT": "CT",
    "MST": "MT",
    "MDT": "MT",
    "PST": "PT",
    "PDT": "PT",
    "AKST": "AKT",
    "AKDT": "AKT",
    "HST": "HT",
    "HDT": "HT",
}


def get_tz_abbreviation(dt):
    """Return a simplified timezone abbreviation for a datetime.

    US timezones are simplified (e.g. EST/EDT -> ET). Others are returned as-is.
    """
    abbr = dt.strftime("%Z")
    return _TZ_ABBR_SIMPLIFY.get(abbr, abbr)


_RE_YEAR_4 = re.compile(r" Y(?= |$)")
_RE_YEAR_2 = re.compile(r"/y(?= |$)")
_RE_TRAILING_COMMA = re.compile(r",\s*$")


def strip_current_year_from_format(fmt, dt):
    """Remove year format characters from fmt if dt falls in the current year.

    Handles Django date format strings. Removes:
      - ' Y' (4-digit year, e.g. "M d, Y g:i A" -> "M d, g:i A")
      - '/y' (2-digit year, e.g. "n/j/y" -> "n/j")
    """
    if dt.year != date.today().year:
        return fmt
    fmt = _RE_YEAR_4.sub("", fmt)
    fmt = _RE_YEAR_2.sub("", fmt)
    return _RE_TRAILING_COMMA.sub("", fmt)


def get_user_tz(user):
    """Return ZoneInfo for the given user, defaulting to `settings.TIME_ZONE`."""
    if user and getattr(user, "time_zone", None):
        try:
            return ZoneInfo(user.time_zone)
        except KeyError, ValueError:
            pass
    return timezone.get_default_timezone()


def format_datetime_with_tz(dt, fmt, tz=None):
    """Format a datetime in the given timezone with a tz abbreviation appended.

    Uses strftime format strings. If tz is None, uses the currently active timezone
    (or America/New_York if none is active).
    """
    local_dt = timezone.localtime(dt, tz) if tz else timezone.localtime(dt)
    tz_abbr = get_tz_abbreviation(local_dt)
    return f"{local_dt.strftime(fmt)} {tz_abbr}"
