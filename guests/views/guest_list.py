"""The guest list editor at /manage/guests/: households as cards, guests inside them.

Server-rendered DaisyUI with HTMX. Every write re-renders the household card it touched (two cards for a
move, swapped out of band) and fires `guestlist-changed` so the totals refresh. Drag-and-drop is SortableJS
(`static/js/guest-list.js`), which posts the new order to the reorder endpoints; the up/down buttons do the
same through the same JS, so keyboards and phones get the same behaviour. Confirmations render into the
page's one `<dialog>` (`#gl-modal-body`), never a browser dialog. The rules live in `guests/guest_list.py`.
"""

from functools import wraps

import structlog
from django.contrib.admin.views.decorators import staff_member_required as _staff_member_required
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render, resolve_url
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_htmx.http import HttpResponseClientRedirect, reswap, retarget, trigger_client_event

from core.types import AppRequest
from guests import guest_list
from guests.forms.guest_list import AddGuestForm, GuestForm, HouseholdForm, QuickAddForm
from guests.models import Event, Guest, Household
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

MODAL_TARGET = "#gl-modal-body"
CHANGED = "guestlist-changed"
CLOSE_MODAL = "gl-close-modal"


# --- Helpers ---------------------------------------------------------------------------------


def staff_member_required(view):
    """`staff_member_required`, but an HTMX request from an expired session reloads the whole page to the
    login, instead of following the redirect and swapping the login page into a card or the dialog."""
    protected = _staff_member_required(view)

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.htmx and not (request.user.is_active and request.user.is_staff):
            logger.warning("guest_list_session_expired", path=request.path)
            return HttpResponseClientRedirect(f"{resolve_url('admin:login')}?next={resolve_url('manage:guest_list')}")
        return protected(request, *args, **kwargs)

    return wrapper


def _guests_prefetch() -> Prefetch:
    return Prefetch("guests", queryset=Guest.objects.order_by("sort_order", "id").select_related("table"))


def _households(queryset=None):
    queryset = Household.objects.all() if queryset is None else queryset
    return queryset.prefetch_related(_guests_prefetch(), "invited_events")


def _load(pk: int) -> Household:
    return get_object_or_404(_households(), pk=pk)


def _events() -> list[Event]:
    return list(Event.objects.order_by("order", "starts_at"))


def _card_context(
    household: Household, *, events=None, notice: str = "", oob: bool = False, add_open: bool = False
) -> dict:
    invited = {event.pk for event in household.invited_events.all()}
    add_guest_form = AddGuestForm(auto_id=f"add-{household.pk}-%s")
    if add_open:
        # Adding several guests in a row: keep the form open and the cursor in it.
        add_guest_form.fields["first_name"].widget.attrs["autofocus"] = True
    return {
        "household": household,
        "guests": list(household.guests.all()),
        "event_chips": [{"event": event, "invited": event.pk in invited} for event in events or _events()],
        "has_plus_one": any(guest.is_plus_one for guest in household.guests.all()),
        "add_guest_form": add_guest_form,
        "add_open": add_open,
        "notice": notice,
        "oob": oob,
    }


def _render_card(request, household: Household, **kwargs) -> str:
    return render_to_string("guests/manage/guest_list/_household.html", _card_context(household, **kwargs), request)


def _changed(response: HttpResponse, *, close_modal: bool = False) -> HttpResponse:
    trigger_client_event(response, CHANGED, after="swap")
    if close_modal:
        trigger_client_event(response, CLOSE_MODAL, after="swap")
    return response


def _card_response(
    request, pk: int, *, notice: str = "", close_modal: bool = False, add_open: bool = False
) -> HttpResponse:
    response = HttpResponse(_render_card(request, _load(pk), notice=notice, add_open=add_open))
    return _changed(response, close_modal=close_modal)


def _oob_cards_response(request, *pks: int, close_modal: bool = True) -> HttpResponse:
    events = _events()
    households = {household.pk: household for household in _households().filter(pk__in=pks)}
    html = "".join(_render_card(request, households[pk], events=events, oob=True) for pk in pks if pk in households)
    return _changed(HttpResponse(html), close_modal=close_modal)


def _modal(request, template: str, context: dict) -> HttpResponse:
    """Render into the dialog, whatever element the request targeted."""
    response = render(request, f"guests/manage/guest_list/{template}", context)
    return reswap(retarget(response, MODAL_TARGET), "innerHTML")


def _ids(request, key: str = "order") -> list[int] | None:
    try:
        return [int(value) for value in request.POST.getlist(key) if value]
    except ValueError:
        return None


# --- The page --------------------------------------------------------------------------------


@staff_member_required
@require_GET
def guest_list_page(request: AppRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    side = request.GET.get("side", "")
    households = list(_households(guest_list.search_households(q=q, side=side)))
    events = _events()
    context = {
        "cards": [_card_context(household, events=events) for household in households],
        "q": q,
        "side": side,
        "sides": Household.Side.choices,
        "filtered": bool(q or side in Household.Side.values),
        "total_households": Household.objects.count(),
    }
    if request.htmx and request.htmx.target == "household-list":
        return render(request, "guests/manage/guest_list/_list.html", context)
    context |= {
        "totals": guest_list.totals(),
        "quick_add": QuickAddForm(),
        "codes_frozen": WeddingSettings.load().codes_frozen,
    }
    return render(request, "guests/manage/guest_list/page.html", context)


@staff_member_required
@require_GET
def totals(request: AppRequest) -> HttpResponse:
    return render(request, "guests/manage/guest_list/_totals.html", {"totals": guest_list.totals()})


# --- Households ------------------------------------------------------------------------------


@staff_member_required
@require_POST
def household_add(request: AppRequest) -> HttpResponse:
    form = QuickAddForm(request.POST)
    if not form.is_valid():
        return render(request, "guests/manage/guest_list/_quick_add.html", {"quick_add": form})
    household = guest_list.create_household(
        text=form.cleaned_data["text"], side=form.cleaned_data["side"], events=form.cleaned_data["events"]
    )
    context = {
        "quick_add": QuickAddForm(initial={"side": form.cleaned_data["side"]}),
        "new_card": _render_card(request, _load(household.pk)),
        "autofocus": True,
    }
    return _changed(render(request, "guests/manage/guest_list/_quick_add.html", context))


@staff_member_required
@require_POST
def households_reorder(request: AppRequest) -> HttpResponse:
    order = _ids(request)
    if not order:
        return HttpResponseBadRequest("Send the new order as repeated `order` ids.")
    try:
        guest_list.reorder_households(order)
    except ValueError:
        logger.warning("guest_list_reorder_rejected", kind="households", count=len(order))
        return HttpResponseBadRequest("The order names a household that no longer exists. Reload the page.")
    return HttpResponse(status=204)


@staff_member_required
@require_GET
def household_card(request: AppRequest, pk: int) -> HttpResponse:
    return HttpResponse(_render_card(request, _load(pk)))


@staff_member_required
@require_http_methods(["GET", "POST"])
def household_edit(request: AppRequest, pk: int) -> HttpResponse:
    household = _load(pk)
    form = HouseholdForm(request.POST or None, instance=household, auto_id=f"h{pk}-%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        logger.info("household_updated", household_id=pk, fields=form.changed_data, source="guest_list")
        return _card_response(request, pk)
    return render(request, "guests/manage/guest_list/_household_form.html", {"household": household, "form": form})


@staff_member_required
@require_http_methods(["GET", "POST"])
def household_delete(request: AppRequest, pk: int) -> HttpResponse:
    household = get_object_or_404(Household, pk=pk)
    stakes = guest_list.household_stakes(household)
    context = {
        "title": f"Delete {household.name}?",
        "subject": household.name,
        "detail": f"{household.guests.count()} guest(s) and the invite code {household.invite_code} go with it.",
        "stakes": stakes,
        "action": request.path,
    }
    if request.method == "POST":
        if stakes and not guest_list.confirmation_matches(request.POST.get("confirm_name", ""), household.name):
            context["error"] = "Type the household name exactly to confirm."
            return _modal(request, "_confirm_delete.html", context)
        guest_list.delete_household(household)
        response = HttpResponse(f'<div id="household-{pk}" hx-swap-oob="delete"></div>')
        return _changed(response, close_modal=True)
    return _modal(request, "_confirm_delete.html", context)


@staff_member_required
@require_POST
def household_plus_one(request: AppRequest, pk: int) -> HttpResponse:
    household = get_object_or_404(Household, pk=pk)
    notice = guest_list.set_plus_one(household, on=request.POST.get("on") == "1") or ""
    return _card_response(request, pk, notice=notice)


@staff_member_required
@require_POST
def household_event(request: AppRequest, pk: int, event_pk: int) -> HttpResponse:
    household = get_object_or_404(Household, pk=pk)
    event = get_object_or_404(Event, pk=event_pk)
    invited = request.POST.get("invited") == "1"
    if not invited and not request.POST.get("confirm"):
        lost = guest_list.rsvps_lost_by_uninvite(household, event)
        if lost:
            return _modal(
                request,
                "_confirm_uninvite.html",
                {"household": household, "event": event, "lost": lost, "action": request.path},
            )
    guest_list.set_invitation(household, event, invited=invited)
    return _card_response(request, pk, close_modal=True)


# --- Guests ----------------------------------------------------------------------------------


@staff_member_required
@require_POST
def guest_add(request: AppRequest, pk: int) -> HttpResponse:
    household = get_object_or_404(Household, pk=pk)
    form = AddGuestForm(request.POST, instance=Guest(household=household), auto_id=f"add-{pk}-%s")
    if not form.is_valid():
        context = _card_context(_load(pk))
        context["add_guest_form"] = form
        return render(request, "guests/manage/guest_list/_household.html", context)
    guest = form.save(commit=False)
    guest.sort_order = max(household.guests.values_list("sort_order", flat=True), default=0) + 1
    guest.save()
    logger.info("guest_added", guest_id=guest.pk, household_id=pk, source="guest_list")
    return _card_response(request, pk, add_open=True)


@staff_member_required
@require_POST
def guests_reorder(request: AppRequest, pk: int) -> HttpResponse:
    household = get_object_or_404(Household, pk=pk)
    order = _ids(request)
    try:
        if order is None:
            raise ValueError("bad ids")
        guest_list.reorder_guests(household, order)
    except ValueError:
        logger.warning("guest_list_reorder_rejected", kind="guests", household_id=pk)
        # The page is stale (someone else changed this household): show the truth.
        return HttpResponse(_render_card(request, _load(pk), notice="The list had changed; here it is again."))
    return HttpResponse(status=204)


@staff_member_required
@require_http_methods(["GET", "POST"])
def guest_edit(request: AppRequest, pk: int) -> HttpResponse:
    guest = get_object_or_404(Guest, pk=pk)
    form = GuestForm(request.POST or None, instance=guest, auto_id=f"g{pk}-%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        logger.info("guest_updated", guest_id=pk, fields=form.changed_data, source="guest_list")
        return _card_response(request, guest.household_id)
    return render(request, "guests/manage/guest_list/_guest_form.html", {"guest": guest, "form": form})


@staff_member_required
@require_http_methods(["GET", "POST"])
def guest_delete(request: AppRequest, pk: int) -> HttpResponse:
    guest = get_object_or_404(Guest.objects.select_related("household", "table"), pk=pk)
    stakes = guest_list.guest_stakes(guest)
    context = {
        "title": f"Remove {guest.display_name}?",
        "subject": guest.display_name,
        "detail": f"From {guest.household.name}. The household stays, even if it is left empty.",
        "stakes": stakes,
        "action": request.path,
    }
    if request.method == "POST":
        if stakes and not guest_list.confirmation_matches(request.POST.get("confirm_name", ""), guest.display_name):
            context["error"] = "Type the guest's name exactly to confirm."
            return _modal(request, "_confirm_delete.html", context)
        household = guest_list.delete_guest(guest)
        return _oob_cards_response(request, household.pk)
    return _modal(request, "_confirm_delete.html", context)


@staff_member_required
@require_http_methods(["GET", "POST"])
def guest_move(request: AppRequest, pk: int) -> HttpResponse:
    """GET: the "move to…" picker. POST `household` (+ `order` from a drop, `confirm` after the warning)."""
    guest = get_object_or_404(Guest.objects.select_related("household"), pk=pk)
    if request.method == "GET":
        others = Household.objects.exclude(pk=guest.household_id).order_by("name").only("pk", "name")
        return _modal(request, "_move.html", {"guest": guest, "households": others, "action": request.path})

    try:
        target = Household.objects.get(pk=int(request.POST.get("household", "")))
    except ValueError, Household.DoesNotExist:
        logger.warning("guest_move_rejected", guest_id=pk, reason="unknown_household")
        return _oob_cards_response(request, guest.household_id)
    if target.pk == guest.household_id:
        return _oob_cards_response(request, guest.household_id)
    order = _ids(request)
    if not request.POST.get("confirm"):
        lost = guest_list.rsvps_lost_by_move(guest, target)
        if lost:
            context = {"guest": guest, "target": target, "lost": lost, "order": order or [], "action": request.path}
            return _modal(request, "_confirm_move.html", context)
    source = guest_list.move_guest(guest, target, order=order)
    return _oob_cards_response(request, source.pk, target.pk)
