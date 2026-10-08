"""Session keys shared by the save-the-date, RSVP, and schedule views.

Nothing identifies a guest except these. `SESSION_UNLOCK_KEY` for the site password
lives in `pages.middleware`.
"""

RSVP_HOUSEHOLD_SESSION_KEY = "rsvp_household_id"
RSVP_CANDIDATES_SESSION_KEY = "rsvp_candidates"


def set_rsvp_household(request, household_id: int) -> None:
    request.session[RSVP_HOUSEHOLD_SESSION_KEY] = household_id
    request.session.pop(RSVP_CANDIDATES_SESSION_KEY, None)


def get_rsvp_household_id(request) -> int | None:
    return request.session.get(RSVP_HOUSEHOLD_SESSION_KEY)


def clear_rsvp_session(request) -> None:
    request.session.pop(RSVP_HOUSEHOLD_SESSION_KEY, None)
    request.session.pop(RSVP_CANDIDATES_SESSION_KEY, None)
