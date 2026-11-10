"""The rules behind the guest list editor at /manage/guests/.

The views in `guests/views/guest_list.py` only parse requests and render cards; every change to
the list goes through a function here, so the rules stay in one place:

- Household order is `Household.sort_order`, renumbered from 1 on every reorder. New households
  are 0, so they sit at the top until someone drags them into place. A reorder may name only the
  households currently on screen (a search or side filter): they are put back into the slots they
  already occupied, in the new order, and everything off screen keeps its position.
- Guest order inside a household is `Guest.sort_order`, renumbered from 1.
- Moving a guest to another household drops their RSVPs for events the new household is not
  invited to (and per-guest answers to questions conditional on those events); per-guest answers
  follow the guest. The caller asks `rsvps_lost_by_move()` first so the person can confirm.
- Un-inviting a household from an event drops its RSVPs to that event the same way.
- A household left empty is kept: its invite code may already be in a save-the-date link.
"""

import re
from dataclasses import dataclass, field

import structlog
from django.db import transaction
from django.db.models import Count, Q

from guests.models import CampaignRecipient, Event, Guest, Household, Rsvp, RsvpAnswer, split_names
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

# "Bob Jones & Guest", "Bob Jones + 1", "Bob Jones and guest".
_PLUS_ONE_SUFFIX = re.compile(r"\s*(?:&|\+|\band\b)\s*(?:a\s+)?(?:guest|1|one)\s*$", re.IGNORECASE)
# "The Smiths" names a household, not a person.
_COLLECTIVE_NAME = re.compile(r"^the\s", re.IGNORECASE)


# --- Quick add -------------------------------------------------------------------------------


def parse_quick_add(text: str) -> tuple[list[tuple[str, str]], bool]:
    """Turn "Bob & Carol Jones & Guest" into ([("Bob", "Jones"), ("Carol", "Jones")], True).

    "The Smiths" yields no guests: the person adds them on the card.
    """
    text = " ".join((text or "").split())
    plus_one = bool(_PLUS_ONE_SUFFIX.search(text))
    names = _PLUS_ONE_SUFFIX.sub("", text)
    if not names or _COLLECTIVE_NAME.match(names):
        return [], plus_one
    return split_names(names), plus_one


@transaction.atomic
def create_household(*, text: str, side: str, events) -> Household:
    pairs, plus_one = parse_quick_add(text)
    household = Household.objects.create(name=" ".join(text.split()), side=side)
    household.invited_events.set(events)
    for position, (first, last) in enumerate(pairs, start=1):
        Guest.objects.create(household=household, first_name=first, last_name=last, sort_order=position)
    if plus_one:
        Guest.objects.create(household=household, is_plus_one=True, sort_order=len(pairs) + 1)
    logger.info("household_created", household_id=household.pk, guests=len(pairs) + plus_one, source="guest_list")
    return household


# --- Ordering --------------------------------------------------------------------------------


def _dedupe(ids) -> list[int]:
    seen: set[int] = set()
    return [pk for pk in ids if not (pk in seen or seen.add(pk))]


@transaction.atomic
def reorder_households(visible_ids: list[int]) -> None:
    """Apply a new order to the households on screen, keeping everything else where it is."""
    visible_ids = _dedupe(visible_ids)
    current = list(Household.objects.select_for_update().order_by("sort_order", "name").values_list("pk", "sort_order"))
    known = {pk for pk, _ in current}
    if unknown := set(visible_ids) - known:
        raise ValueError(f"Unknown households: {sorted(unknown)}")
    visible = set(visible_ids)
    incoming = iter(visible_ids)
    new_order = [next(incoming) if pk in visible else pk for pk, _ in current]
    old_positions = dict(current)
    changed = [
        Household(pk=pk, sort_order=position)
        for position, pk in enumerate(new_order, start=1)
        if old_positions[pk] != position
    ]
    Household.objects.bulk_update(changed, ["sort_order"])


def _renumber_guests(household: Household, ordered_ids: list[int]) -> None:
    guests = {guest.pk: guest for guest in household.guests.all()}
    changed = []
    for position, pk in enumerate(ordered_ids, start=1):
        guest = guests[pk]
        if guest.sort_order != position:
            guest.sort_order = position
            changed.append(guest)
    Guest.objects.bulk_update(changed, ["sort_order"])


def _current_guest_ids(household: Household) -> list[int]:
    return list(household.guests.order_by("sort_order", "id").values_list("pk", flat=True))


@transaction.atomic
def reorder_guests(household: Household, ordered_ids: list[int]) -> None:
    ordered_ids = _dedupe(ordered_ids)
    if set(ordered_ids) != set(_current_guest_ids(household)):
        raise ValueError("The order must list exactly the household's guests.")
    _renumber_guests(household, ordered_ids)


# --- Moving guests and changing invitations --------------------------------------------------


def rsvps_lost_by_move(guest: Guest, target: Household) -> list[Rsvp]:
    """The guest's RSVPs for events the target household is not invited to."""
    return list(guest.rsvps.exclude(event__in=target.invited_events.all()).select_related("event"))


@transaction.atomic
def move_guest(guest: Guest, target: Household, *, order: list[int] | None = None) -> Household:
    """Move `guest` into `target`, at the position `order` gives (or last). Returns the old household."""
    source = guest.household
    if source.pk == target.pk:
        raise ValueError("The guest is already in that household.")
    lost = rsvps_lost_by_move(guest, target)
    lost_event_ids = [rsvp.event_id for rsvp in lost]
    Rsvp.objects.filter(pk__in=[rsvp.pk for rsvp in lost]).delete()
    RsvpAnswer.objects.filter(guest=guest, question__only_if_attending__in=lost_event_ids).delete()
    RsvpAnswer.objects.filter(guest=guest).update(household=target)

    guest.household = target
    guest.save(update_fields=("household", "updated_at"))

    target_ids = [pk for pk in _current_guest_ids(target) if pk != guest.pk]
    if order is not None and set(_dedupe(order)) == {*target_ids, guest.pk}:
        target_ids = _dedupe(order)
    else:
        target_ids.append(guest.pk)
    _renumber_guests(target, target_ids)
    _renumber_guests(source, _current_guest_ids(source))
    logger.info(
        "guest_moved",
        guest_id=guest.pk,
        from_household_id=source.pk,
        to_household_id=target.pk,
        rsvps_removed=len(lost),
    )
    return source


def rsvps_lost_by_uninvite(household: Household, event: Event) -> list[Rsvp]:
    return list(Rsvp.objects.filter(guest__household=household, event=event).select_related("guest"))


@transaction.atomic
def set_invitation(household: Household, event: Event, *, invited: bool) -> int:
    """Invite or un-invite the household. Un-inviting deletes its RSVPs to the event; returns how many."""
    if invited:
        household.invited_events.add(event)
        removed = 0
    else:
        household.invited_events.remove(event)
        removed, _ = Rsvp.objects.filter(guest__household=household, event=event).delete()
        RsvpAnswer.objects.filter(household=household, question__only_if_attending=event).delete()
    logger.info(
        "household_invitation_changed",
        household_id=household.pk,
        event_id=event.pk,
        invited=invited,
        rsvps_removed=removed,
    )
    return removed


# --- Plus-ones -------------------------------------------------------------------------------


def removable_plus_ones(household: Household) -> list[Guest]:
    """Plus-ones the toggle may delete: still unnamed, with no RSVP or answer."""
    return list(
        household.guests.filter(is_plus_one=True, first_name="", last_name="")
        .annotate(rsvp_count=Count("rsvps", distinct=True), answer_count=Count("rsvp_answers", distinct=True))
        .filter(rsvp_count=0, answer_count=0)
    )


@transaction.atomic
def set_plus_one(household: Household, *, on: bool) -> str | None:
    """Add or remove the household's unnamed guest. Returns a message when it cannot be removed."""
    plus_ones = household.guests.filter(is_plus_one=True)
    if on:
        if not plus_ones.exists():
            last = max(_current_guest_sort_orders(household), default=0)
            Guest.objects.create(household=household, is_plus_one=True, sort_order=last + 1)
            logger.info("plus_one_added", household_id=household.pk)
        return None
    removable = removable_plus_ones(household)
    kept = plus_ones.exclude(pk__in=[guest.pk for guest in removable]).count()
    Guest.objects.filter(pk__in=[guest.pk for guest in removable]).delete()
    if removable:
        logger.info("plus_one_removed", household_id=household.pk, removed=len(removable))
    if kept:
        return "The plus-one has a name or an RSVP now. Delete them from the list below if they're not coming."
    return None


def _current_guest_sort_orders(household: Household) -> list[int]:
    return list(household.guests.values_list("sort_order", flat=True))


# --- Deletion --------------------------------------------------------------------------------


@dataclass
class Stakes:
    """What a delete would lose. Truthy when there is anything; then the person types the name."""

    lines: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.lines)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def household_stakes(household: Household) -> Stakes:
    stakes = Stakes()
    rsvps = Rsvp.objects.filter(guest__household=household).count()
    answers = household.rsvp_answers.count()
    if rsvps:
        stakes.lines.append(f"{_plural(rsvps, 'RSVP')} from its guests will be deleted.")
    if answers:
        stakes.lines.append(f"{_plural(answers, 'RSVP answer')} will be deleted.")
    if household.responded_at and not rsvps:
        stakes.lines.append("They have responded to the RSVP.")
    if household.campaign_recipients.filter(status=CampaignRecipient.Status.SENT).exists():
        stakes.lines.append("We have emailed them; the link in that email will stop working.")
    elif WeddingSettings.load().codes_frozen:
        stakes.lines.append("Invite codes are frozen, so their save-the-date link may already be out.")
    if household.address_confirmed_at:
        stakes.lines.append("Their confirmed mailing address will be lost.")
    return stakes


def guest_stakes(guest: Guest) -> Stakes:
    stakes = Stakes()
    rsvps = guest.rsvps.count()
    answers = guest.rsvp_answers.count()
    if rsvps:
        stakes.lines.append(f"{_plural(rsvps, 'RSVP')} will be deleted.")
    if answers:
        stakes.lines.append(f"{_plural(answers, 'RSVP answer')} will be deleted.")
    if guest.table_id:
        stakes.lines.append(f"They are seated at {guest.table}.")
    return stakes


def confirmation_matches(typed: str, expected: str) -> bool:
    return " ".join((typed or "").split()).casefold() == " ".join(expected.split()).casefold()


def delete_household(household: Household) -> None:
    pk, guests = household.pk, household.guests.count()
    household.delete()
    logger.info("household_deleted", household_id=pk, guests=guests)


def delete_guest(guest: Guest) -> Household:
    household = guest.household
    pk = guest.pk
    guest.delete()
    _renumber_guests(household, _current_guest_ids(household))
    logger.info("guest_deleted", guest_id=pk, household_id=household.pk)
    return household


# --- Reading the list ------------------------------------------------------------------------


def search_households(*, q: str = "", side: str = ""):
    households = Household.objects.order_by("sort_order", "name")
    if side in Household.Side.values:
        households = households.filter(side=side)
    if q := " ".join(q.split()):
        matches = Q(name__icontains=q) | Q(invite_code__iexact=q)
        for term in q.split():
            matches |= Q(guests__first_name__icontains=term) | Q(guests__last_name__icontains=term)
        households = households.filter(matches).distinct()
    return households


def totals() -> dict:
    guest_counts = Guest.objects.aggregate(
        guests=Count("id"),
        plus_ones=Count("id", filter=Q(is_plus_one=True)),
        children=Count("id", filter=Q(is_child=True)),
    )
    by_side = {
        row["side"]: row
        for row in Household.objects.values("side")
        .annotate(households=Count("id", distinct=True), guests=Count("guests", distinct=True))
        .order_by()
    }
    sides = [
        {
            "label": label,
            "households": by_side.get(value, {}).get("households", 0),
            "guests": by_side.get(value, {}).get("guests", 0),
        }
        for value, label in Household.Side.choices
    ]
    return {
        "households": Household.objects.count(),
        "empty_households": Household.objects.filter(guests__isnull=True).count(),
        **guest_counts,
        "sides": sides,
    }
