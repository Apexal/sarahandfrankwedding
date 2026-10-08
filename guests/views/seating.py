"""Reception seating: the chart with its warnings, and the one-table-per-page print view.

Seating is reception-only (`Guest.table`). Assignment happens in the Guest admin; this page
shows the result and what needs attention. A declined guest keeps the seat and is flagged,
never unseated, so a late "actually we can come" does not lose it. A guest with no reception
`Rsvp` row is neither unassigned nor a warning until they respond.
"""

from itertools import groupby

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode

from core.types import AppRequest
from guests import exports
from guests.models import Guest, Table

STATUS_ATTENDING = "attending"
STATUS_DECLINED = "declined"
STATUS_NO_RSVP = "no_rsvp"


def _guest_admin_url(guest: Guest) -> str:
    return reverse("admin:guests_guest_change", args=[guest.pk])


def _guest_status(guest: Guest, *, attending: set[int], declined: set[int]) -> str:
    if guest.pk in attending:
        return STATUS_ATTENDING
    if guest.pk in declined:
        return STATUS_DECLINED
    return STATUS_NO_RSVP


def _group_by_household(guests, *, meals: dict[int, str], attending: set[int], declined: set[int]) -> list[dict]:
    """`guests` ordered by household name then sort order → one entry per household."""
    grouped = []
    for household, members in groupby(guests, key=lambda guest: guest.household):
        grouped.append(
            {
                "household": household,
                "guests": [
                    {
                        "guest": guest,
                        "meal": meals.get(guest.pk, ""),
                        "status": _guest_status(guest, attending=attending, declined=declined),
                        "admin_url": _guest_admin_url(guest),
                    }
                    for guest in members
                ],
            }
        )
    return grouped


def build_seating() -> dict:
    """Everything the seating page and its print view render."""
    reception = exports.reception_event()
    attending = exports.rsvp_guest_ids(reception, attending=True)
    declined = exports.rsvp_guest_ids(reception, attending=False)
    meal_question = exports.meal_question()
    meals = exports.answers_by_guest(meal_question)
    seated, unassigned = exports.seating_guests(reception)

    tables = list(Table.objects.order_by("order", "name"))
    guests_by_table: dict[int, list[Guest]] = {table.pk: [] for table in tables}
    for guest in seated:
        guests_by_table[guest.table_id].append(guest)

    warnings: list[dict] = []
    cards = []
    for table in tables:
        guests = guests_by_table[table.pk]
        used = len(guests)
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
                "households": _group_by_household(guests, meals=meals, attending=attending, declined=declined),
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

    assign_url = reverse("admin:guests_guest_changelist")
    if reception is not None:
        assign_url = f"{assign_url}?{urlencode({'attending': reception.pk})}"

    return {
        "reception": reception,
        "cards": cards,
        "unassigned": _group_by_household(unassigned, meals=meals, attending=attending, declined=declined),
        "unassigned_count": len(unassigned),
        "warnings": warnings,
        "seated_count": len(seated),
        "attending_count": len(attending),
        "capacity": sum(table.capacity for table in tables),
        "meal_question": meal_question,
        "assign_url": assign_url,
    }


@staff_member_required
def seating(request: AppRequest) -> HttpResponse:
    return render(request, "guests/manage/seating.html", build_seating())


@staff_member_required
def seating_print(request: AppRequest) -> HttpResponse:
    return render(request, "guests/manage/seating_print.html", build_seating())
