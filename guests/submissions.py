"""Reviewing addresses typed into the generic save-the-date page (`ContactSubmission`).

The names are free text, so review stays manual: we either merge a submission into the household it
belongs to (`ContactSubmission.merge_into()`, the one merge path) or make a new household from it.
`suggest_households()` only ranks likely matches for us to pick from; it never merges on its own.
"""

from django.db import transaction
from django.db.models import Q

from core.utils.phone import normalize_phone_number
from guests.models import ContactSubmission, Guest, Household, normalize_name, split_names


class SubmissionError(Exception):
    """The submission cannot be handled as asked; the message is fit to show us."""


def create_household(submission: ContactSubmission) -> Household:
    """Make a household named as typed, with one guest per name, and merge the submission into it."""
    if submission.status != ContactSubmission.Status.NEW:
        raise SubmissionError("This submission has already been handled.")
    name = " ".join(submission.names.split())
    if Household.objects.filter(name__iexact=name).exists():
        raise SubmissionError(f"A household named “{name}” already exists. Merge into it instead.")
    with transaction.atomic():
        household = Household.objects.create(name=name)
        for order, (first, last) in enumerate(split_names(name)):
            Guest.objects.create(household=household, first_name=first, last_name=last, sort_order=order)
        submission.merge_into(household)
    return household


def merge(submission: ContactSubmission, household: Household) -> None:
    if submission.status != ContactSubmission.Status.NEW:
        raise SubmissionError("This submission has already been handled.")
    with transaction.atomic():
        submission.merge_into(household)


def suggest_households(submission: ContactSubmission, limit: int = 5) -> list[Household]:
    """Households that probably sent this: same email or phone first, then a shared last name."""
    conditions = Q()
    if submission.email:
        conditions |= Q(email__iexact=submission.email)
    phone = normalize_phone_number(submission.phone)
    if phone:
        conditions |= Q(phone=phone)
    last_names = {normalize_name(last) for _, last in split_names(submission.names) if last}
    last_names.discard("")
    for last in last_names:
        conditions |= Q(guests__last_name_normalized=last) | Q(name__icontains=last)
    if not conditions:
        return []
    candidates = list(Household.objects.filter(conditions).distinct().prefetch_related("guests")[:50])

    def score(household: Household) -> tuple:
        exact_contact = (submission.email and household.email.lower() == submission.email.lower()) or (
            phone and household.phone == phone
        )
        return (not exact_contact, household.address_confirmed, household.name)

    return sorted(candidates, key=score)[:limit]
