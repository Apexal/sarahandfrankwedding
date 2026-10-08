"""CSV exports shared by the admin actions and the /manage/ dashboard."""

import csv

from django.db.models import Prefetch
from django.http import HttpResponse

from guests.models import Event, Guest, Household, Rsvp, RsvpQuestion
from pages.models import WeddingSettings


def csv_response(filename: str, header: list[str], rows) -> HttpResponse:
    """A CSV download with a BOM so Excel opens UTF-8 correctly."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


INVITE_CODES_HEADER = [
    "household",
    "invite_code",
    "side",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
    "country",
]


def invite_codes_rows(households):
    for h in households.order_by("name"):
        yield [
            h.name,
            h.invite_code,
            h.side,
            h.address_line1,
            h.address_line2,
            h.city,
            h.state,
            h.postal_code,
            h.country,
        ]


SAVE_THE_DATE_LINKS_HEADER = ["household", "contact_names", "phone", "email", "link", "message"]


def save_the_date_links_rows(households, wedding: WeddingSettings | None = None):
    """One row per household with the prewritten text message to paste from our phones."""
    wedding = wedding or WeddingSettings.load()
    for household in households.prefetch_related("guests").order_by("name"):
        yield [
            household.name,
            household.contact_names,
            household.phone,
            household.email,
            household.save_the_date_url,
            household.save_the_date_message(wedding),
        ]


# --- RSVPs (pivoted per guest) ---------------------------------------------------------

TEXT_KINDS = (RsvpQuestion.Kind.SHORT_TEXT, RsvpQuestion.Kind.LONG_TEXT)


def answer_text(value) -> str:
    """Flatten a stored answer into one cell: lists join with "; ", booleans read yes / no."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def attending_text(attending: bool | None) -> str:
    """ "yes" / "no", or "" while the (guest, event) has no `Rsvp` row."""
    if attending is None:
        return ""
    return "yes" if attending else "no"


def rsvp_events() -> list[Event]:
    return list(Event.objects.filter(rsvp_required=True))


def active_questions() -> list[RsvpQuestion]:
    return list(RsvpQuestion.objects.filter(active=True))


def rsvps_header(events, questions) -> list[str]:
    return [
        "household",
        "side",
        "guest",
        "is_child",
        *(f"attending:{event.slug}" for event in events),
        *(f"q:{question.label}" for question in questions),
        "household_email",
        "responded_at",
    ]


def rsvps_rows(households, events, questions):
    """One row per guest.

    Per-guest answers land on the guest's row; per-household answers are repeated on every
    row of the household so the sheet filters cleanly. An event column is "" until the
    guest has an `Rsvp` row for it (pending is the absence of a row).
    """
    households = households.prefetch_related(
        Prefetch("guests", queryset=Guest.objects.order_by("sort_order", "id").prefetch_related("rsvps")),
        "rsvp_answers",
    ).order_by("name")
    for household in households:
        answers = household.rsvp_answers.all()
        household_answers = {answer.question_id: answer.value for answer in answers if answer.guest_id is None}
        guest_answers = {(answer.guest_id, answer.question_id): answer.value for answer in answers if answer.guest_id}
        responded_at = household.responded_at.isoformat() if household.responded_at else ""
        for guest in household.guests.all():
            rsvps = {rsvp.event_id: rsvp.attending for rsvp in guest.rsvps.all()}
            answer_cells = []
            for question in questions:
                if question.scope == RsvpQuestion.Scope.PER_HOUSEHOLD:
                    answer_cells.append(answer_text(household_answers.get(question.pk)))
                else:
                    answer_cells.append(answer_text(guest_answers.get((guest.pk, question.pk))))
            yield [
                household.name,
                household.side,
                guest.display_name,
                "yes" if guest.is_child else "no",
                *(attending_text(rsvps.get(event.pk)) for event in events),
                *answer_cells,
                household.email,
                responded_at,
            ]


# --- Seating ---------------------------------------------------------------------------

SEATING_HEADER = ["table", "guest", "household", "is_child", "meal", "dietary"]
MEAL_LABEL_HINT = "meal"
DIETARY_LABEL_HINT = "diet"


def reception_event() -> Event | None:
    """The event seating is for: the "reception" slug if it exists, else the first RSVP-required event."""
    return Event.objects.filter(slug="reception").first() or Event.objects.filter(rsvp_required=True).first()


def find_guest_question(kinds, hint: str) -> RsvpQuestion | None:
    """The first active per-guest question of one of `kinds` whose label contains `hint`."""
    return (
        RsvpQuestion.objects.filter(
            active=True, scope=RsvpQuestion.Scope.PER_GUEST, kind__in=kinds, label__icontains=hint
        )
        .order_by("order", "id")
        .first()
    )


def meal_question() -> RsvpQuestion | None:
    return find_guest_question([RsvpQuestion.Kind.SINGLE_CHOICE], MEAL_LABEL_HINT)


def dietary_question() -> RsvpQuestion | None:
    return find_guest_question(TEXT_KINDS, DIETARY_LABEL_HINT)


def answers_by_guest(question: RsvpQuestion | None) -> dict[int, str]:
    """guest id → flattened answer; empty when there is no such question."""
    if question is None:
        return {}
    return {
        guest_id: answer_text(value)
        for guest_id, value in question.answers.filter(guest__isnull=False).values_list("guest_id", "value")
    }


def rsvp_guest_ids(event: Event | None, *, attending: bool) -> set[int]:
    if event is None:
        return set()
    return set(Rsvp.objects.filter(event=event, attending=attending).values_list("guest_id", flat=True))


def seating_guests(reception: Event | None) -> tuple[list[Guest], list[Guest]]:
    """(seated, unassigned): seated guests by table order / name, household name, sort order;
    then guests attending the reception with no table, by household name and sort order.

    Seated guests are listed whatever their RSVP says: a declined guest keeps the seat and
    the seating page flags it. A guest with no reception `Rsvp` row is not unassigned yet.
    """
    seated = list(
        Guest.objects.filter(table__isnull=False)
        .select_related("household", "table")
        .order_by("table__order", "table__name", "household__name", "sort_order", "id")
    )
    if reception is None:
        return seated, []
    unassigned = list(
        Guest.objects.filter(table__isnull=True, rsvps__event=reception, rsvps__attending=True)
        .select_related("household")
        .order_by("household__name", "sort_order", "id")
    )
    return seated, unassigned


def seating_rows(guests, *, meals: dict[int, str], dietary: dict[int, str]):
    """`guests` in output order (see `seating_guests`); unassigned guests carry table ""."""
    for guest in guests:
        yield [
            guest.table.name if guest.table_id else "",
            guest.display_name,
            guest.household.name,
            "yes" if guest.is_child else "no",
            meals.get(guest.pk, ""),
            dietary.get(guest.pk, ""),
        ]


__all__ = [
    "DIETARY_LABEL_HINT",
    "INVITE_CODES_HEADER",
    "MEAL_LABEL_HINT",
    "SAVE_THE_DATE_LINKS_HEADER",
    "SEATING_HEADER",
    "TEXT_KINDS",
    "Household",
    "active_questions",
    "answer_text",
    "answers_by_guest",
    "attending_text",
    "csv_response",
    "dietary_question",
    "find_guest_question",
    "invite_codes_rows",
    "meal_question",
    "reception_event",
    "rsvp_events",
    "rsvp_guest_ids",
    "rsvps_header",
    "rsvps_rows",
    "save_the_date_links_rows",
    "seating_guests",
    "seating_rows",
]
