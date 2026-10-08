"""The digital save-the-date: one page, two entrances, plus the calendar file (PLAN.md section 10).

Both entrances are exempt from the site password (`SitePasswordMiddleware.EXEMPT_NAMESPACES`)
and call `unlock_site()`, so a guest holding a save-the-date link never meets the password
page. The personalized entrance also records the household in the session for the RSVP flow.

Each entrance has a `letter/` twin that renders the same page with the letter already open and
no envelope, so the letter and its form can be worked on without replaying the animation.
"""

import structlog
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.http import content_disposition_header

from core.ratelimit import ratelimit
from core.types import AppRequest
from guests.forms.address import GenericAddressForm, HouseholdAddressForm
from guests.ics import CONTENT_TYPE as ICS_CONTENT_TYPE
from guests.ics import ics_filename, render_ics
from guests.models import Household
from guests.session import set_rsvp_household
from pages.middleware import unlock_site
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

TEMPLATE = "guests/save_the_date/page.html"
# The form posts with HTMX and swaps just this in place; see the template's comment.
REPLY_TEMPLATE = "guests/save_the_date/_reply.html"


def _render(
    request: AppRequest,
    form,
    *,
    household: Household | None = None,
    submitted: bool = False,
    letter: bool = False,
):
    """Render the page. It opens straight to the letter on a `letter/` URL and after any POST:
    a guest fixing an error or reading the thank-you must not have to open the envelope again.
    An HTMX post gets only the form or thank-you, which it swaps into the open letter."""
    return render(
        request,
        REPLY_TEMPLATE if request.htmx else TEMPLATE,
        {
            "form": form,
            "household": household,
            "submitted": submitted,
            "opened": letter or submitted or form.is_bound,
            "source": "personalized" if household else "generic",
        },
    )


@ratelimit("10/h")
def personalized(request: AppRequest, code: str, letter: bool = False) -> HttpResponse:
    """`/save-the-date/<code>/`: greet the household, prefill, and save back onto it."""
    household = get_object_or_404(Household.objects.prefetch_related("guests"), invite_code=code.upper())
    unlock_site(request)
    set_rsvp_household(request, household.pk)

    if request.method == "POST":
        form = HouseholdAddressForm(request.POST, instance=household)
        if form.is_valid():
            form.save()
            logger.info("address_submitted", source="personalized", household_id=household.pk)
            return _render(request, form, household=household, submitted=True, letter=letter)
        logger.info("address_form_invalid", source="personalized", household_id=household.pk)
    else:
        form = HouseholdAddressForm(instance=household)
        logger.info("save_the_date_viewed", source="personalized", household_id=household.pk, letter=letter)

    return _render(request, form, household=household, letter=letter)


@ratelimit("10/h")
def generic(request: AppRequest, letter: bool = False) -> HttpResponse:
    """`/save-the-date/`: the forwardable page. Live only while `save_the_date_open`."""
    if not WeddingSettings.load().save_the_date_open:
        raise Http404("The save-the-date page is not open.")
    unlock_site(request)

    if request.method == "POST":
        form = GenericAddressForm(request.POST)
        if form.tripped:
            # A bot filled the hidden field. Say thanks, save nothing, tell nobody.
            logger.info("address_form_honeypot", source="generic")
            return _render(request, GenericAddressForm(), submitted=True, letter=letter)
        if form.is_valid():
            submission = form.save(ip=request.META.get("REMOTE_ADDR") or None)
            logger.info("address_submitted", source="generic", submission_id=submission.pk)
            return _render(request, form, submitted=True, letter=letter)
        logger.info("address_form_invalid", source="generic")
    else:
        form = GenericAddressForm()
        logger.info("save_the_date_viewed", source="generic", letter=letter)

    return _render(request, form, letter=letter)


def ics(request: AppRequest) -> HttpResponse:
    """`/save-the-date/event.ics`: the all-day event as a calendar attachment."""
    wedding = WeddingSettings.load()
    response = HttpResponse(render_ics(wedding), content_type=ICS_CONTENT_TYPE)
    response.headers["Content-Disposition"] = content_disposition_header(True, ics_filename(wedding.couple_names))
    return response
