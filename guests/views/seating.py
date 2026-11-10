"""Reception seating: the visual editor with its warnings, and the one-table-per-page print view.

Seating is reception-only (`Guest.table`). `/manage/seating/` draws each table as a ring of seats
with its guests beneath, next to a pool of unseated guests; guests are dragged between them
(SortableJS, `static/js/seating.js`) or seated from a dialog, which is the keyboard and phone path.
Every write goes through `guests/seating.py` and answers with the whole board re-rendered, so the
counts, rings, and warnings never drift from the database. Dialogs reuse the guest list editor's
`<dialog>` plumbing (`static/js/guest-list.js`).

A declined guest keeps the seat and is flagged, never unseated, so a late "actually we can come"
does not lose it. A guest with no reception `Rsvp` row is in the pool (we can plan before RSVPs
arrive) but is not a warning until they respond.
"""

import math
from itertools import groupby

import structlog
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_htmx.http import reswap, retarget, trigger_client_event

from core.types import AppRequest
from guests import exports
from guests import seating as rules
from guests.forms.seating import TableForm
from guests.models import Guest, Table
from guests.views.guest_list import staff_member_required

logger = structlog.get_logger(__name__)

STATUS_ATTENDING = "attending"
STATUS_DECLINED = "declined"
STATUS_NO_RSVP = "no_rsvp"

BOARD_TEMPLATE = "guests/manage/seating/_board.html"
MODAL_TARGET = "#gl-modal-body"
CLOSE_MODAL = "gl-close-modal"

# The seat ring: seats sit on a circle at this radius, in percent of the ring's box.
RING_RADIUS = 41
# Past this many seats the dots shrink so neighbours don't overlap.
LARGE_TABLE = 12


def _guest_admin_url(guest: Guest) -> str:
    return reverse("admin:guests_guest_change", args=[guest.pk])


def _guest_status(guest: Guest, *, attending: set[int], declined: set[int]) -> str:
    if guest.pk in attending:
        return STATUS_ATTENDING
    if guest.pk in declined:
        return STATUS_DECLINED
    return STATUS_NO_RSVP


def _initials(guest: Guest) -> str:
    if not guest.full_name:
        return "+1"
    return "".join(part[0] for part in (guest.first_name, guest.last_name) if part).upper()


def _seat(guest: Guest, *, meals: dict[int, str], attending: set[int], declined: set[int]) -> dict:
    return {
        "guest": guest,
        "meal": meals.get(guest.pk, ""),
        "status": _guest_status(guest, attending=attending, declined=declined),
        "admin_url": _guest_admin_url(guest),
        "initials": _initials(guest),
    }


def _group_by_household(guests, **seat_kwargs) -> list[dict]:
    """`guests` ordered by household → one entry per household."""
    grouped = []
    for household, members in groupby(guests, key=lambda guest: guest.household):
        seats = [_seat(guest, **seat_kwargs) for guest in members]
        grouped.append(
            {
                "household": household,
                "guests": seats,
                "guest_ids": " ".join(str(seat["guest"].pk) for seat in seats),
            }
        )
    return grouped


def _ring(seats: list[dict], capacity: int) -> list[dict]:
    """One dot per seat, clockwise from twelve o'clock: guests first, then empty chairs.

    An over-full table gets extra dots, marked `extra`, so the overflow is visible at a glance.
    """
    count = max(capacity, len(seats), 1)
    dots = []
    for index in range(count):
        angle = 2 * math.pi * index / count - math.pi / 2
        dots.append(
            {
                "seat": seats[index] if index < len(seats) else None,
                "extra": index >= capacity,
                "x": round(50 + RING_RADIUS * math.cos(angle), 2),
                "y": round(50 + RING_RADIUS * math.sin(angle), 2),
            }
        )
    return dots


def _pool(reception, *, declined: set[int]) -> list[Guest]:
    """Unseated guests we might still seat: invited to the reception and not declined it."""
    guests = Guest.objects.filter(table__isnull=True).select_related("household")
    if reception is not None:
        guests = guests.filter(household__invited_events=reception).exclude(pk__in=declined)
    return list(guests.order_by("household__name", "household_id", "sort_order", "id"))


def build_seating() -> dict:
    """Everything the seating editor and its print view render."""
    reception = exports.reception_event()
    attending = exports.rsvp_guest_ids(reception, attending=True)
    declined = exports.rsvp_guest_ids(reception, attending=False)
    meal_question = exports.meal_question()
    meals = exports.answers_by_guest(meal_question)
    seat_kwargs = {"meals": meals, "attending": attending, "declined": declined}
    seated, unassigned = exports.seating_guests(reception)

    tables = list(Table.objects.order_by("order", "name"))
    guests_by_table: dict[int, list[Guest]] = {table.pk: [] for table in tables}
    for guest in seated:
        guests_by_table[guest.table_id].append(guest)

    warnings: list[dict] = []
    cards = []
    for table in tables:
        households = _group_by_household(guests_by_table[table.pk], **seat_kwargs)
        seats = [seat for group in households for seat in group["guests"]]
        used = len(seats)
        over = used > table.capacity
        if over:
            warnings.append(
                {
                    "kind": "over_capacity",
                    "message": f"{table.name} is over capacity: {used} seated, {table.capacity} seats.",
                    "url": reverse("admin:guests_table_change", args=[table.pk]),
                }
            )
        cards.append(
            {
                "table": table,
                "used": used,
                "free": max(table.capacity - used, 0),
                "over": over,
                "percent": round(100 * used / table.capacity) if table.capacity else 0,
                "bar_max": max(table.capacity, 1),
                "households": households,
                "seats": seats,
                "ring": _ring(seats, table.capacity),
                "large": max(table.capacity, used) > LARGE_TABLE,
            }
        )

    tables_by_household: dict[int, dict] = {}
    for guest in seated:
        entry = tables_by_household.setdefault(guest.household_id, {"household": guest.household, "tables": {}})
        entry["tables"][guest.table_id] = guest.table.name
    for entry in tables_by_household.values():
        if len(entry["tables"]) > 1:
            household = entry["household"]
            names = ", ".join(sorted(entry["tables"].values()))
            warnings.append(
                {
                    "kind": "split_household",
                    "message": f"{household.name} is split across {names}.",
                    "url": f"{reverse('admin:guests_guest_changelist')}?{urlencode({'q': household.name})}",
                }
            )

    if reception is not None:
        for guest in seated:
            status = _guest_status(guest, attending=attending, declined=declined)
            if status == STATUS_ATTENDING:
                continue
            reason = "declined the reception" if status == STATUS_DECLINED else "has not RSVPed for the reception"
            warnings.append(
                {
                    "kind": "seated_not_attending",
                    "message": f"{guest.display_name} ({guest.household.name}) {reason} but is seated at "
                    f"{guest.table.name}.",
                    "url": _guest_admin_url(guest),
                }
            )
        for guest in unassigned:
            warnings.append(
                {
                    "kind": "unseated",
                    "message": f"{guest.display_name} ({guest.household.name}) is attending but has no table.",
                    "url": _guest_admin_url(guest),
                }
            )

    pool = _group_by_household(_pool(reception, declined=declined), **seat_kwargs)
    return {
        "reception": reception,
        "cards": cards,
        "unassigned": _group_by_household(unassigned, **seat_kwargs),
        "unassigned_count": len(unassigned),
        "pool": pool,
        "pool_count": sum(len(group["guests"]) for group in pool),
        "warnings": warnings,
        "seated_count": len(seated),
        "attending_count": len(attending),
        "capacity": sum(table.capacity for table in tables),
        "meal_question": meal_question,
    }


# --- Responses -------------------------------------------------------------------------------


def _board(request, *, notice: str = "", close_modal: bool = False) -> HttpResponse:
    context = build_seating() | {"notice": notice}
    response = HttpResponse(render_to_string(BOARD_TEMPLATE, context, request))
    if close_modal:
        trigger_client_event(response, CLOSE_MODAL, after="swap")
    return response


def _modal(request, template: str, context: dict) -> HttpResponse:
    """Render into the dialog, whatever element the request targeted."""
    response = render(request, f"guests/manage/seating/{template}", context)
    return reswap(retarget(response, MODAL_TARGET), "innerHTML")


def _ids(request, key: str) -> list[int] | None:
    try:
        return [int(value) for value in request.POST.getlist(key) if value]
    except ValueError:
        return None


# --- Pages -----------------------------------------------------------------------------------


@staff_member_required
@require_GET
def seating(request: AppRequest) -> HttpResponse:
    if request.htmx and request.htmx.target == "seating-board":
        return _board(request)
    return render(request, "guests/manage/seating.html", build_seating())


@staff_member_required
@require_GET
def seating_print(request: AppRequest) -> HttpResponse:
    return render(request, "guests/manage/seating_print.html", build_seating())


# --- Assigning -------------------------------------------------------------------------------


@staff_member_required
@require_POST
def seat_assign(request: AppRequest) -> HttpResponse:
    """POST one or more `guest` ids and a `table` id (blank to unseat). Answers with the board."""
    guest_ids = _ids(request, "guest")
    if not guest_ids:
        return HttpResponseBadRequest("Send one or more `guest` ids.")
    table = None
    if table_id := request.POST.get("table", "").strip():
        table = Table.objects.filter(pk=table_id).first() if table_id.isdigit() else None
        if table is None:
            logger.warning("seat_assign_rejected", reason="unknown_table", guests=len(guest_ids))
            return _board(request, notice="That table no longer exists; here is the current seating.")
    try:
        rules.assign(guest_ids, table)
    except ValueError:
        logger.warning("seat_assign_rejected", reason="unknown_guest", guests=len(guest_ids))
        return _board(request, notice="A guest had been removed; here is the current seating.", close_modal=True)
    return _board(request, close_modal=True)


@staff_member_required
@require_GET
def seat_guest(request: AppRequest, pk: int) -> HttpResponse:
    """The "seat at…" dialog for one guest: every table with its free seats, unseat, the household."""
    guest = get_object_or_404(Guest.objects.select_related("household", "table"), pk=pk)
    tables = list(Table.objects.order_by("order", "name").prefetch_related("guests"))
    household = [other for other in guest.household.guests.order_by("sort_order", "id") if other.pk != guest.pk]
    context = {
        "guest": guest,
        "tables": [{"table": table, "used": len(table.guests.all())} for table in tables],
        "household_ids": [guest.pk] + [other.pk for other in household],
        "household_others": household,
    }
    return _modal(request, "_seat.html", context)


# --- Tables ----------------------------------------------------------------------------------


@staff_member_required
@require_POST
def tables_reorder(request: AppRequest) -> HttpResponse:
    order = _ids(request, "order")
    try:
        if not order:
            raise ValueError("no ids")
        rules.reorder_tables(order)
    except ValueError:
        logger.warning("tables_reorder_rejected", count=len(order or []))
        return _board(request, notice="The tables had changed; here they are again.")
    return HttpResponse(status=204)


def _table_form_response(request, form: TableForm, table: Table | None) -> HttpResponse:
    return _modal(request, "_table_form.html", {"form": form, "table": table, "action": request.path})


@staff_member_required
@require_http_methods(["GET", "POST"])
def table_add(request: AppRequest) -> HttpResponse:
    form = TableForm(request.POST or None, initial={"capacity": 10}, auto_id="table-%s")
    if request.method == "POST" and form.is_valid():
        table = form.save(commit=False)
        table.order = rules.next_table_order()
        table.save()
        logger.info("table_created", table_id=table.pk, capacity=table.capacity, source="seating")
        return _board(request, close_modal=True)
    if request.method == "GET" and not form.initial.get("name"):
        form.initial["name"] = f"Table {Table.objects.count() + 1}"
    return _table_form_response(request, form, None)


@staff_member_required
@require_http_methods(["GET", "POST"])
def table_edit(request: AppRequest, pk: int) -> HttpResponse:
    table = get_object_or_404(Table, pk=pk)
    form = TableForm(request.POST or None, instance=table, auto_id=f"table{pk}-%s")
    if request.method == "POST" and form.is_valid():
        form.save()
        logger.info("table_updated", table_id=pk, fields=form.changed_data, source="seating")
        return _board(request, close_modal=True)
    return _table_form_response(request, form, table)


@staff_member_required
@require_http_methods(["GET", "POST"])
def table_delete(request: AppRequest, pk: int) -> HttpResponse:
    table = get_object_or_404(Table, pk=pk)
    if request.method == "POST":
        rules.delete_table(table)
        return _board(request, close_modal=True)
    guests = list(table.guests.select_related("household").order_by("household__name", "sort_order"))
    return _modal(request, "_confirm_delete_table.html", {"table": table, "guests": guests, "action": request.path})
