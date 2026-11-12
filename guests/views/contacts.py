"""The contact sheet at /manage/contacts/: every household on one scrolling table, contact fields as cells.

Built for typing in a lot of emails, phone numbers, and addresses at once. Each cell saves on its own when
it changes (an HTMX post of that one field) and comes back re-rendered, so the phone shows normalized and a
bad email shows its error in place. Rows are households because that is where contact details live: the
household email is what campaigns send to, and the envelope goes to the household address. Keyboard
movement between rows is `static/js/contacts.js`.
"""

import structlog
from django.db.models import Count, Q
from django.forms import modelform_factory
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_htmx.http import trigger_client_event

from core.types import AppRequest
from core.utils.phone import normalize_phone_number
from guests import guest_list
from guests.models import Guest, Household
from guests.views.guest_list import _guests_prefetch, staff_member_required

logger = structlog.get_logger(__name__)

CHANGED = "contacts-changed"

#: The editable columns, in order: field name → (header, input type, width class, wanted).
#: A "wanted" cell is tinted while it is empty, so the gaps stand out while scrolling.
COLUMNS = {
    "email": ("Email", "email", "w-60", True),
    "phone": ("Phone", "tel", "w-36", True),
    "address_line1": ("Address", "text", "w-56", True),
    "address_line2": ("Apt / line 2", "text", "w-32", False),
    "city": ("City", "text", "w-36", True),
    "state": ("State", "text", "w-20", True),
    "postal_code": ("ZIP", "text", "w-24", True),
    "country": ("Country", "text", "w-16", False),
    "address_confirmed": ("Confirmed", "checkbox", "", False),
}

MISSING_CHOICES = [
    ("", "All"),
    ("any", "Missing something"),
    ("email", "No email"),
    ("phone", "No phone"),
    ("address", "No address"),
    ("unconfirmed", "Address unconfirmed"),
]

_NO_ADDRESS = Q(address_line1="") | Q(city="") | Q(postal_code="")
_MISSING = {
    "any": Q(email="") | Q(phone="") | _NO_ADDRESS,
    "email": Q(email=""),
    "phone": Q(phone=""),
    "address": _NO_ADDRESS,
    "unconfirmed": Q(address_confirmed_at__isnull=True),
}


def _households(*, q: str = "", side: str = "", missing: str = ""):
    households = guest_list.search_households(q=q, side=side)
    if missing in _MISSING:
        households = households.filter(_MISSING[missing])
    return households.prefetch_related(_guests_prefetch())


def _cell(household: Household, field: str, *, value=None, error: str = "", saved: bool = False) -> dict:
    label, input_type, width, wanted = COLUMNS[field]
    if value is None:
        value = household.address_confirmed if field == "address_confirmed" else getattr(household, field)
    return {
        "household": household,
        "field": field,
        "label": label,
        "type": input_type,
        "width": width,
        "wanted": wanted,
        "value": value,
        "error": error,
        "saved": saved,
        "bounced": field == "email" and household.email_bounced_at is not None,
    }


def _row(household: Household) -> dict:
    guests = [guest.display_name for guest in household.guests.all()]
    return {"household": household, "guests": guests, "cells": [_cell(household, field) for field in COLUMNS]}


def _summary() -> dict:
    counts = Household.objects.aggregate(
        total=Count("id"),
        email=Count("id", filter=~Q(email="")),
        phone=Count("id", filter=~Q(phone="")),
        address=Count("id", filter=~_NO_ADDRESS),
        confirmed=Count("id", filter=Q(address_confirmed_at__isnull=False)),
    )
    counts["guests"] = Guest.objects.count()
    return counts


@staff_member_required
@require_GET
def contacts(request: AppRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    side = request.GET.get("side", "")
    missing = request.GET.get("missing", "")
    target = request.htmx.target if request.htmx else None
    if target == "contacts-summary":
        return render(request, "guests/manage/contacts/_summary.html", {"summary": _summary()})
    rows = [_row(household) for household in _households(q=q, side=side, missing=missing)]
    context = {"rows": rows, "filtered": bool(q or side or missing)}
    if target == "contact-rows":
        return render(request, "guests/manage/contacts/_rows.html", context)
    context |= {
        "q": q,
        "side": side,
        "missing": missing,
        "sides": Household.Side.choices,
        "missing_choices": MISSING_CHOICES,
        "columns": [(field, label, width) for field, (label, _type, width, _wanted) in COLUMNS.items()],
        "summary": _summary(),
    }
    return render(request, "guests/manage/contacts/page.html", context)


@staff_member_required
@require_POST
def contact_cell(request: AppRequest, pk: int, field: str) -> HttpResponse:
    """Save one cell. A checkbox that is unticked sends nothing, so an absent value means off/blank."""
    if field not in COLUMNS:
        raise Http404("Not an editable contact field.")
    household = get_object_or_404(Household.objects.prefetch_related(_guests_prefetch()), pk=pk)

    if field == "address_confirmed":
        confirmed = request.POST.get(field) in ("on", "1", "true")
        if confirmed != household.address_confirmed:
            household.address_confirmed_at = timezone.now() if confirmed else None
            household.save(update_fields=("address_confirmed_at", "updated_at"))
            logger.info("household_updated", household_id=pk, fields=[field], source="contacts")
        return _cell_response(request, household, field, saved=True)

    raw = request.POST.get(field, "")
    previous = getattr(household, field)
    # `is_valid()` writes the cleaned value onto the instance, so `previous` is read first.
    form = modelform_factory(Household, fields=(field,))({field: raw}, instance=household)
    if not form.is_valid():
        return _cell_response(request, household, field, value=raw, error=" ".join(form.errors[field]))
    if field == "phone" and raw.strip() and not normalize_phone_number(raw):
        return _cell_response(request, household, field, value=raw, error="Enter a phone number.")

    household = form.save(commit=False)
    update_fields = [field, "updated_at"]
    if field == "email" and household.email_bounced_at and household.email.casefold() != previous.casefold():
        # The bounce belonged to the old address; campaigns may try the new one.
        household.email_bounced_at = None
        update_fields.append("email_bounced_at")
    household.save(update_fields=update_fields)
    logger.info(
        "household_updated", household_id=pk, fields=[f for f in update_fields if f != "updated_at"], source="contacts"
    )
    return _cell_response(request, household, field, saved=True)


def _cell_response(request, household: Household, field: str, **kwargs) -> HttpResponse:
    response = render(request, "guests/manage/contacts/_cell.html", {"cell": _cell(household, field, **kwargs)})
    if kwargs.get("saved"):
        trigger_client_event(response, CHANGED, after="swap")
    return response
