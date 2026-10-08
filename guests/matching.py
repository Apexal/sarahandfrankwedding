"""Household lookup for the RSVP flow: fuzzy name matching and the invite-code path.

PLAN.md section 9. Every guest is loaded once per lookup (a few hundred rows at most)
and everything below runs in Python, so it behaves identically on SQLite and Postgres.
A last-name match is required before a first name is even scored, which is what keeps
the lookup from being an enumeration tool. The invite code bypasses matching entirely.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

from guests.models import Guest, Household, normalize_name
from guests.nicknames import are_nicknames

LAST_NAME_RATIO = 85
FIRST_NAME_RATIO = 80
EXACT_SCORE = 100
NICKNAME_SCORE = 95
MAX_CANDIDATES = 3


@dataclass(frozen=True, slots=True)
class HouseholdMatch:
    household: Household
    score: int
    guest: Guest


def last_name_matches(typed: str, stored: str) -> bool:
    """`typed` and `stored` are already normalized.

    A whole-token hit ("garcia" against "smith garcia") or a close whole-string /
    whole-token ratio. `ratio` rather than `partial_ratio` so "lee" is not "leeds".
    """
    if not typed or not stored:
        return False
    if typed == stored:
        return True
    tokens = stored.split()
    if typed in tokens:
        return True
    if fuzz.ratio(typed, stored) >= LAST_NAME_RATIO:
        return True
    return any(fuzz.ratio(typed, token) >= LAST_NAME_RATIO for token in tokens)


def first_name_score(typed: str, guest: Guest) -> int | None:
    """Score a normalized typed first name against one guest, or None to reject."""
    stored = guest.first_name_normalized
    if not typed or not stored:
        return None
    if typed == stored or typed in stored.split():
        return EXACT_SCORE
    if are_nicknames(typed, stored) or any(are_nicknames(typed, token) for token in stored.split()):
        return NICKNAME_SCORE
    for alias in guest.aliases:
        if typed == alias or typed in alias.split():
            return NICKNAME_SCORE
    ratio = int(fuzz.ratio(typed, stored))
    return ratio if ratio >= FIRST_NAME_RATIO else None


def match_households(first_name: str, last_name: str) -> list[HouseholdMatch]:
    """Rank households by their best-matching guest, at most `MAX_CANDIDATES` of them."""
    typed_first = normalize_name(first_name)
    typed_last = normalize_name(last_name)
    if not typed_first or not typed_last:
        return []

    best: dict[int, HouseholdMatch] = {}
    for guest in Guest.objects.select_related("household").order_by("household__name", "sort_order", "id"):
        if not last_name_matches(typed_last, guest.last_name_normalized):
            continue
        score = first_name_score(typed_first, guest)
        if score is None:
            continue
        current = best.get(guest.household_id)
        if current is None or score > current.score:
            best[guest.household_id] = HouseholdMatch(household=guest.household, score=score, guest=guest)

    ranked = sorted(best.values(), key=lambda match: (-match.score, match.household.name))
    return ranked[:MAX_CANDIDATES]


def find_households(first_name: str, last_name: str) -> list[Household]:
    return [match.household for match in match_households(first_name, last_name)]


def normalize_invite_code(code: str) -> str:
    """Codes are typed from a card: ignore case and any spaces or dashes."""
    return "".join(ch for ch in (code or "") if ch.isalnum()).upper()


def find_household_by_code(code: str) -> Household | None:
    normalized = normalize_invite_code(code)
    if not normalized:
        return None
    return Household.objects.filter(invite_code=normalized).first()
