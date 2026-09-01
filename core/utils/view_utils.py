from datetime import datetime

from django.utils import timezone


def parse_iso_datetime(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def parse_optional_iso_datetime(value):
    if not value:
        return None
    try:
        return parse_iso_datetime(value)
    except ValueError:
        return None


def parse_calendar_range(request):
    return (
        parse_optional_iso_datetime(request.GET.get("start")),
        parse_optional_iso_datetime(request.GET.get("end")),
    )
