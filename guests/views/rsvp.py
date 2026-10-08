"""The RSVP flow: lookup → select → respond → done (PLAN.md section 9).

No login. The session carries the household (see `guests.session`); nothing else
identifies a guest. `WeddingSettings.rsvp_is_open(event)` is the only open/closed
decision: while closed, every step renders the closed message and, if the session has
a household, a read-only summary of what they told us.
"""

import structlog
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.ratelimit import ratelimit
from core.types import AppRequest
from guests import session as rsvp_session
from guests.email_contexts import (
    RSVP_CONFIRMATION,
    RSVP_RECEIVED_INTERNAL,
    RsvpConfirmationContext,
    RsvpReceivedInternalContext,
    common_fields,
    ics_url,
    rsvp_deadline_display,
    rsvp_url,
    site_url,
)
from guests.forms.rsvp import CodeLookupForm, NameLookupForm, build_rsvp_form, open_rsvp_events
from guests.matching import find_household_by_code, find_households
from guests.models import Event, Household, Rsvp, RsvpAnswer
from messaging.services import EmailTemplateService, EmailTemplateServiceError
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

# --- Shared helpers --------------------------------------------------------------------


def _session_household(request: AppRequest) -> Household | None:
    household_id = rsvp_session.get_rsvp_household_id(request)
    if household_id is None:
        return None
    household = Household.objects.filter(pk=household_id).first()
    if household is None:
        # The household was deleted or merged since the link was sent; forget it.
        rsvp_session.clear_rsvp_session(request)
    return household


def _is_open(wedding: WeddingSettings, household: Household | None = None) -> bool:
    """Open for this household, or (with no household) for anyone at all."""
    if not wedding.rsvp_open:
        return False
    if household is not None:
        return bool(open_rsvp_events(household, wedding))
    required_events = list(Event.objects.filter(rsvp_required=True))
    if not required_events:
        return wedding.rsvp_is_open()
    return any(wedding.rsvp_is_open(event) for event in required_events)


def _closed(request: AppRequest, household: Household | None) -> HttpResponse:
    return render(
        request,
        "guests/rsvp/closed.html",
        {"household": household, "summary": household_summary(household) if household else None},
    )


def display_value(value) -> str:
    """A stored `RsvpAnswer.value` (string, list, or bool) as text."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def household_summary(household: Household) -> dict:
    """What the household has told us, in the shape the confirmation email and the pages share.

    `guests[].answers[].attending` is None for an invited event without an answer yet;
    the email builder drops those.
    """
    guests = list(household.guests.order_by("sort_order", "id"))
    events = [event for event in household.invited_events.all() if event.rsvp_required]
    attendance = {(r.guest_id, r.event_id): r.attending for r in Rsvp.objects.filter(guest__household=household)}
    answers = RsvpAnswer.objects.filter(household=household, question__active=True).select_related(
        "question", "guest", "guest__household"
    )
    return {
        "guests": [
            {
                "name": guest.display_name,
                "answers": [
                    {"event": event.name, "attending": attendance.get((guest.pk, event.pk))} for event in events
                ],
            }
            for guest in guests
        ],
        "question_answers": [
            {
                "label": answer.question.label,
                "guest": answer.guest.display_name if answer.guest else None,
                "value": display_value(answer.value),
            }
            for answer in answers
        ],
    }


def _candidate_rows(candidates: list[Household]) -> list[dict]:
    """Household name and the first names of its named guests: nothing else leaves the server."""
    rows = []
    for household in candidates:
        first_names = [g.first_name for g in household.guests.all() if g.first_name and not g.is_plus_one]
        rows.append({"household": household, "first_names": " & ".join(first_names)})
    return rows


def _select(request: AppRequest, household: Household) -> HttpResponse:
    rsvp_session.set_rsvp_household(request, household.pk)
    logger.info("rsvp_household_selected", household_id=household.pk)
    return redirect("rsvp:respond")


def _record_lookup(*, method: str, candidates: list[Household], first_name: str = "", last_name: str = "") -> None:
    matched = bool(candidates)
    logger.info(
        "rsvp_lookup",
        method=method,
        matched=matched,
        candidate_count=len(candidates),
        household_ids=[household.pk for household in candidates],
        first_name=first_name,
        last_name=last_name,
    )


# --- Emails ----------------------------------------------------------------------------


def _summary_lines(guests: list[dict], question_answers: list[dict], email: str) -> list[str]:
    lines = []
    for guest in guests:
        answers = ", ".join(f"{a['event']} {'yes' if a['attending'] else 'no'}" for a in guest["answers"])
        lines.append(f"{guest['name']}: {answers or 'no answers yet'}")
    for answer in question_answers:
        who = f" ({answer['guest']})" if answer["guest"] else ""
        lines.append(f"{answer['label']}{who}: {answer['value']}")
    lines.append(f"Email: {email}" if email else "Email: none given")
    return lines


def _send(service: EmailTemplateService, *, template_key: str, to_email: str, context, household: Household) -> None:
    """Queue one email; a template problem must not undo an RSVP that is already saved."""
    reply_to = WeddingSettings.load().contact_email or None
    try:
        service.send_email(template_key=template_key, to_email=to_email, context_data=context, reply_to=reply_to)
    except EmailTemplateServiceError as exc:
        logger.exception(
            "rsvp_email_failed", template_key=template_key, household_id=household.pk, error_type=type(exc).__name__
        )


def _notify(household: Household, wedding: WeddingSettings, summary: dict, *, to_email: str) -> None:
    """The confirmation to the household (if they gave an email) and the internal notice to us."""
    service = EmailTemplateService()
    answered = [
        {"name": guest["name"], "answers": [a for a in guest["answers"] if a["attending"] is not None]}
        for guest in summary["guests"]
    ]
    if to_email:
        confirmation = RsvpConfirmationContext(
            **common_fields(household, wedding),
            household_name=household.name,
            guests=answered,
            question_answers=summary["question_answers"],
            edit_url=rsvp_url(),
            rsvp_deadline_display=rsvp_deadline_display(wedding),
            ics_url=ics_url(),
        )
        _send(service, template_key=RSVP_CONFIRMATION, to_email=to_email, context=confirmation, household=household)
    if wedding.contact_email:
        internal = RsvpReceivedInternalContext(
            **common_fields(household, wedding),
            household_name=household.name,
            summary_lines=_summary_lines(answered, summary["question_answers"], to_email),
            admin_url=site_url() + reverse("admin:guests_household_change", args=[household.pk]),
        )
        _send(
            service,
            template_key=RSVP_RECEIVED_INTERNAL,
            to_email=wedding.contact_email,
            context=internal,
            household=household,
        )


# --- Views -----------------------------------------------------------------------------


def start(request: AppRequest) -> HttpResponse:
    """The intro and the name form. A session that already knows the household skips to `respond`."""
    wedding = WeddingSettings.load()
    household = _session_household(request)
    if not _is_open(wedding, household):
        return _closed(request, household)
    if household is not None:
        return redirect("rsvp:respond")
    return render(request, "guests/rsvp/start.html", {"name_form": NameLookupForm(), "code_form": CodeLookupForm()})


@require_POST
@ratelimit("20/10m")
def lookup(request: AppRequest) -> HttpResponse:
    """Match the typed name (or look up the code) and offer up to three households."""
    wedding = WeddingSettings.load()
    if not _is_open(wedding):
        return _closed(request, _session_household(request))

    name_form = NameLookupForm()
    code_form = CodeLookupForm()
    if "code" in request.POST:
        code_form = CodeLookupForm(request.POST)
        if not code_form.is_valid():
            return render(request, "guests/rsvp/start.html", {"name_form": name_form, "code_form": code_form})
        household = find_household_by_code(code_form.cleaned_data["code"])
        candidates = [household] if household else []
        _record_lookup(method="code", candidates=candidates)
        if household is not None:
            return _select(request, household)
        method = "code"
    else:
        name_form = NameLookupForm(request.POST)
        if not name_form.is_valid():
            return render(request, "guests/rsvp/start.html", {"name_form": name_form, "code_form": code_form})
        first_name = name_form.cleaned_data["first_name"]
        last_name = name_form.cleaned_data["last_name"]
        candidates = find_households(first_name, last_name)
        _record_lookup(method="name", candidates=candidates, first_name=first_name, last_name=last_name)
        method = "name"

    request.session[rsvp_session.RSVP_CANDIDATES_SESSION_KEY] = [household.pk for household in candidates]
    return render(
        request,
        "guests/rsvp/lookup.html",
        {
            "candidates": _candidate_rows(candidates),
            "method": method,
            "name_form": NameLookupForm(initial=name_form.cleaned_data) if name_form.is_bound else name_form,
            "code_form": CodeLookupForm(),
        },
    )


@require_POST
def select(request: AppRequest, household_id: int) -> HttpResponse:
    """Accept only a household the last lookup offered; anything else is a forged id."""
    wedding = WeddingSettings.load()
    if not _is_open(wedding):
        return _closed(request, _session_household(request))
    offered = request.session.get(rsvp_session.RSVP_CANDIDATES_SESSION_KEY) or []
    if household_id not in offered:
        logger.warning("rsvp_select_rejected", household_id=household_id, offered=offered)
        raise PermissionDenied("That household was not offered by your lookup.")
    return _select(request, get_object_or_404(Household, pk=household_id))


def respond(request: AppRequest) -> HttpResponse:
    """The one form for the household: attendance per guest per event, then the questions."""
    wedding = WeddingSettings.load()
    household = _session_household(request)
    if not _is_open(wedding, household):
        return _closed(request, household)
    if household is None:
        return redirect("rsvp:start")

    form = build_rsvp_form(household, data=request.POST if request.method == "POST" else None, wedding=wedding)
    if request.method == "POST" and form.is_valid():
        first_time = form.save()
        email = (form.cleaned_data.get("contact_email") or "").strip()
        summary = household_summary(household)
        _notify(household, wedding, summary, to_email=email)

        event_name = "rsvp_submitted" if first_time else "rsvp_edited"
        attendance = form.attendance()
        properties = {
            "guests": len(form.guests),
            "events": len(form.events),
            "attending_any": any(attendance.values()),
            "email_provided": bool(email),
        }
        logger.info(event_name, household_id=household.pk, **properties)

        if email:
            messages.success(request, f"Thank you! We emailed a confirmation to {email}.")
        else:
            messages.info(request, "Thank you! No confirmation email was sent because you did not leave an address.")
        return redirect("rsvp:done")

    open_event_ids = {event.pk for event in form.events}
    closed_events = [
        event for event in household.invited_events.all() if event.rsvp_required and event.pk not in open_event_ids
    ]
    return render(
        request,
        "guests/rsvp/respond.html",
        {"household": household, "form": form, "closed_events": closed_events},
    )


def done(request: AppRequest) -> HttpResponse:
    wedding = WeddingSettings.load()
    household = _session_household(request)
    if not _is_open(wedding, household):
        return _closed(request, household)
    if household is None:
        return redirect("rsvp:start")
    return render(
        request,
        "guests/rsvp/done.html",
        {"household": household, "summary": household_summary(household)},
    )


@require_POST
def reset(request: AppRequest) -> HttpResponse:
    """ "Not you? Start over": forget the household and the last lookup."""
    rsvp_session.clear_rsvp_session(request)
    return redirect("rsvp:start")
