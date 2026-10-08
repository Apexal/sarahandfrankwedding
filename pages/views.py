"""Public site views: home, welcome (password), schedule, registry, FAQ, photos, prose pages.

`guests.models` is imported inside the views that need it: `guests` imports this app for
`WeddingSettings`, so a module-level import here would be circular.
"""

from datetime import date

import structlog
from django.db.models import Q, QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme

from core.ratelimit import ratelimit
from core.types import AppRequest
from guests.session import get_rsvp_household_id
from pages.middleware import site_is_unlocked, unlock_site
from pages.models import FaqItem, Page, WeddingSettings

logger = structlog.get_logger(__name__)

WRONG_PASSWORD_MESSAGE = "That password didn't match. Check your invitation and try again."
STALE_LINK_MESSAGE = "That link is out of date. Type the password from your invitation instead."


def _visible_events(request: AppRequest) -> QuerySet:
    """Events this visitor may see: public ones, plus private ones their session household is invited to."""
    from guests.models import Event

    events = Event.objects.order_by("order", "starts_at")
    household_id = get_rsvp_household_id(request)
    if household_id is None:
        return events.filter(is_private=False)
    return events.filter(Q(is_private=False) | Q(households__pk=household_id)).distinct()


def _next_url(request: AppRequest) -> str:
    """The post-unlock destination: a same-host `next`, otherwise home."""
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return reverse("pages:home")


def home(request: AppRequest) -> HttpResponse:
    wedding = WeddingSettings.load()
    days_until = (wedding.wedding_date - timezone.localdate()).days
    return render(
        request,
        "pages/home.html",
        {
            "days_until": days_until,
            "has_schedule": _visible_events(request).exists(),
        },
    )


@ratelimit("10/15m")
def welcome(request: AppRequest) -> HttpResponse:
    """The password page. `?k=<password>` unlocks from a link or QR; the form unlocks on POST."""
    wedding = WeddingSettings.load()
    if not wedding.gate_enabled:
        return redirect("pages:home")

    next_url = _next_url(request)
    error = None

    if request.method == "POST":
        if wedding.check_site_password(request.POST.get("password", "")):
            return _unlocked(request, via="form", next_url=next_url)
        error = WRONG_PASSWORD_MESSAGE
        _password_failed(request, via="form")
    elif "k" in request.GET:
        if wedding.check_site_password(request.GET["k"]):
            return _unlocked(request, via="link", next_url=next_url)
        error = STALE_LINK_MESSAGE
        _password_failed(request, via="link")

    if error is None and site_is_unlocked(request):
        return redirect(next_url)

    return render(request, "pages/welcome.html", {"error": error, "next": next_url}, status=200)


def _unlocked(request: AppRequest, *, via: str, next_url: str) -> HttpResponse:
    unlock_site(request)
    logger.info("site_unlocked", via=via)
    return redirect(next_url)


def _password_failed(request: AppRequest, *, via: str) -> None:
    logger.info("site_password_failed", via=via)


def date_span(days: list[date]) -> str:
    """The days a schedule covers, as its subtitle says them: "September 17 & 18, 2027"."""
    if not days:
        return ""
    first, last = min(days), max(days)
    if first == last:
        return date_format(first, "F j, Y")
    joiner = " & " if len(set(days)) == 2 else " – "
    if first.year != last.year:
        return f"{date_format(first, 'F j, Y')}{joiner}{date_format(last, 'F j, Y')}"
    if first.month != last.month:
        return f"{date_format(first, 'F j')}{joiner}{date_format(last, 'F j, Y')}"
    return f"{date_format(first, 'F j')}{joiner}{date_format(last, 'j, Y')}"


def schedule(request: AppRequest) -> HttpResponse:
    events = list(_visible_events(request))
    days = [timezone.localtime(event.starts_at).date() for event in events]
    subtitle = " · ".join(part for part in (date_span(days), WeddingSettings.load().wedding_city) if part)
    return render(request, "pages/schedule.html", {"events": events, "subtitle": subtitle})


def registry(request: AppRequest) -> HttpResponse:
    return render(request, "pages/registry.html")


def faq(request: AppRequest) -> HttpResponse:
    return render(request, "pages/faq.html", {"items": FaqItem.objects.filter(published=True)})


def photos(request: AppRequest) -> HttpResponse:
    if not WeddingSettings.load().photo_album_url:
        raise Http404("The photo album is not linked yet.")
    return render(request, "pages/photos.html")


def page(request: AppRequest, slug: str) -> HttpResponse:
    prose = get_object_or_404(Page, slug=slug, published=True)
    return render(request, "pages/page.html", {"page": prose})
