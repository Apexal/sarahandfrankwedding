"""Context dataclasses for the wedding's email templates, and the builders that fill them.

Each dataclass is the rendering contract for one `EmailTemplateVersion` row (PLAN.md
section 11). They are registered with `messaging` at import time; `GuestsConfig.ready()`
imports this module so registration happens once Django is up. Every guest-facing email
carries the household's personal save-the-date link and the contact address in its footer,
so a guest who loses both the text and the card can get back in from any email we sent.
"""

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from django.conf import settings

from messaging.template_definitions import TemplateDefinition, preview_defaults, register_template_definition

if TYPE_CHECKING:
    from guests.models import Household
    from pages.models import WeddingSettings

SAVE_THE_DATE = "save_the_date"
ADDRESS_NUDGE = "address_nudge"
RSVP_NUDGE = "rsvp_nudge"
ANNOUNCEMENT = "announcement"
RSVP_CONFIRMATION = "rsvp_confirmation"
RSVP_RECEIVED_INTERNAL = "rsvp_received_internal"

_PREVIEW_SITE_URL = "https://sarahandfrank.wedding"
_PREVIEW_COMMON = {
    "couple_names": "Sarah & Frank",
    "contact_names": "Bob & Carol",
    "save_the_date_url": f"{_PREVIEW_SITE_URL}/save-the-date/K7M2XQ/",
    "site_url": _PREVIEW_SITE_URL,
    "contact_email": "hello@sarahandfrank.wedding",
}
_PREVIEW_SAVE_THE_DATE = {
    **_PREVIEW_COMMON,
    "household_name": "The Joneses",
    "wedding_date_display": "September 18, 2027",
    "wedding_city": "Chicago, Illinois",
    "ics_url": f"{_PREVIEW_SITE_URL}/save-the-date/event.ics",
}


@preview_defaults(_PREVIEW_SAVE_THE_DATE)
@dataclass(frozen=True, slots=True)
class SaveTheDateContext:
    couple_names: str
    contact_names: str
    household_name: str
    wedding_date_display: str
    wedding_city: str
    save_the_date_url: str
    ics_url: str
    site_url: str
    contact_email: str


@preview_defaults(_PREVIEW_SAVE_THE_DATE)
@dataclass(frozen=True, slots=True)
class AddressNudgeContext:
    couple_names: str
    contact_names: str
    household_name: str
    wedding_date_display: str
    wedding_city: str
    save_the_date_url: str
    ics_url: str
    site_url: str
    contact_email: str


@preview_defaults(
    {
        **_PREVIEW_COMMON,
        "rsvp_url": f"{_PREVIEW_SITE_URL}/rsvp/",
        "invite_code": "K7M2XQ",
        "rsvp_deadline_display": "August 1, 2027",
        "site_password": "september",
    }
)
@dataclass(frozen=True, slots=True)
class RsvpNudgeContext:
    couple_names: str
    contact_names: str
    rsvp_url: str
    invite_code: str
    rsvp_deadline_display: str
    site_url: str
    site_password: str
    save_the_date_url: str
    contact_email: str


@preview_defaults(
    {
        **_PREVIEW_COMMON,
        "body_html": "<p>We have booked a block of rooms at the Drake for the weekend of the wedding.</p>",
    }
)
@dataclass(frozen=True, slots=True)
class AnnouncementContext:
    couple_names: str
    contact_names: str
    body_html: str
    site_url: str
    save_the_date_url: str
    contact_email: str


@preview_defaults(
    {
        **_PREVIEW_COMMON,
        "household_name": "The Joneses",
        "guests": [
            {"name": "Bob Jones", "answers": [{"event": "Ceremony", "attending": True}]},
            {"name": "Carol Jones", "answers": [{"event": "Ceremony", "attending": False}]},
        ],
        "question_answers": [
            {"label": "Dietary restrictions", "guest": "Bob Jones", "value": "Vegetarian"},
            {"label": "Song request", "guest": None, "value": "September"},
        ],
        "edit_url": f"{_PREVIEW_SITE_URL}/rsvp/",
        "rsvp_deadline_display": "August 1, 2027",
        "ics_url": f"{_PREVIEW_SITE_URL}/save-the-date/event.ics",
    }
)
@dataclass(frozen=True, slots=True)
class RsvpConfirmationContext:
    couple_names: str
    household_name: str
    contact_names: str
    guests: list[dict]
    question_answers: list[dict]
    edit_url: str
    rsvp_deadline_display: str
    ics_url: str
    site_url: str
    save_the_date_url: str
    contact_email: str


@preview_defaults(
    {
        **_PREVIEW_COMMON,
        "household_name": "The Joneses",
        "summary_lines": ["Bob Jones: Ceremony yes", "Carol Jones: Ceremony no", "Song request: September"],
        "admin_url": f"{_PREVIEW_SITE_URL}/admin/guests/household/1/change/",
    }
)
@dataclass(frozen=True, slots=True)
class RsvpReceivedInternalContext:
    couple_names: str
    contact_names: str
    household_name: str
    summary_lines: list[str]
    admin_url: str
    site_url: str
    save_the_date_url: str
    contact_email: str


TEMPLATE_DEFINITIONS = (
    TemplateDefinition(key=SAVE_THE_DATE, name="Save the date", context_dataclass=SaveTheDateContext),
    TemplateDefinition(key=ADDRESS_NUDGE, name="Address nudge", context_dataclass=AddressNudgeContext),
    TemplateDefinition(key=RSVP_NUDGE, name="RSVP nudge", context_dataclass=RsvpNudgeContext),
    TemplateDefinition(key=ANNOUNCEMENT, name="Announcement", context_dataclass=AnnouncementContext),
    TemplateDefinition(key=RSVP_CONFIRMATION, name="RSVP confirmation", context_dataclass=RsvpConfirmationContext),
    TemplateDefinition(
        key=RSVP_RECEIVED_INTERNAL,
        name="RSVP received (internal)",
        context_dataclass=RsvpReceivedInternalContext,
    ),
)
TEMPLATE_KEYS = tuple(definition.key for definition in TEMPLATE_DEFINITIONS)

for _definition in TEMPLATE_DEFINITIONS:
    register_template_definition(_definition)


# --- Helpers shared by every builder ---------------------------------------------------


def site_url() -> str:
    return settings.APP_BASE_URL.rstrip("/")


def ics_url() -> str:
    return f"{site_url()}/save-the-date/event.ics"


def rsvp_url() -> str:
    return f"{site_url()}/rsvp/"


def _long_date(value: date) -> str:
    return f"{value:%B} {value.day}, {value.year}"


def wedding_date_display(wedding: WeddingSettings) -> str:
    """The date spelled out, "September 18, 2027"."""
    return _long_date(wedding.wedding_date)


def rsvp_deadline_display(wedding: WeddingSettings) -> str:
    """The wedding-wide RSVP deadline spelled out, or "" when none is set."""
    if wedding.rsvp_deadline is None:
        return ""
    return _long_date(wedding.rsvp_deadline)


def common_fields(household: Household, wedding: WeddingSettings) -> dict[str, str]:
    """The fields every context shares: who we are, who they are, and the two links."""
    return {
        "couple_names": wedding.couple_names,
        "contact_names": household.contact_names,
        "save_the_date_url": household.save_the_date_url,
        "site_url": site_url(),
        "contact_email": wedding.contact_email,
    }


def _save_the_date_fields(household: Household, wedding: WeddingSettings) -> dict[str, str]:
    return {
        **common_fields(household, wedding),
        "household_name": household.name,
        "wedding_date_display": wedding_date_display(wedding),
        "wedding_city": wedding.wedding_city,
        "ics_url": ics_url(),
    }


def build_save_the_date_context(household: Household, wedding: WeddingSettings) -> SaveTheDateContext:
    return SaveTheDateContext(**_save_the_date_fields(household, wedding))


def build_address_nudge_context(household: Household, wedding: WeddingSettings) -> AddressNudgeContext:
    return AddressNudgeContext(**_save_the_date_fields(household, wedding))


def build_rsvp_nudge_context(household: Household, wedding: WeddingSettings) -> RsvpNudgeContext:
    return RsvpNudgeContext(
        **common_fields(household, wedding),
        rsvp_url=rsvp_url(),
        invite_code=household.invite_code,
        rsvp_deadline_display=rsvp_deadline_display(wedding),
        site_password=wedding.site_password,
    )


def build_announcement_context(household: Household, wedding: WeddingSettings, body_html: str) -> AnnouncementContext:
    return AnnouncementContext(**common_fields(household, wedding), body_html=body_html)
