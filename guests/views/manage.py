"""The staff dashboard at /manage/, its CSV exports, and the two QR codes.

Everything here is `staff_member_required`; MFA is enforced by `RequireStaffMfaMiddleware`.
The page is server-rendered DaisyUI (stat tiles and progress bars), no chart library.
Counts follow guests/CLAUDE.md: pending is the absence of an `Rsvp` row, address state is
`address_confirmed_at`, and the unit of RSVP is (guest, event).
"""

from collections import Counter

import qrcode
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import urlencode
from qrcode.image.svg import SvgPathImage

from core.types import AppRequest
from guests import exports
from guests.models import (
    Campaign,
    CampaignRecipient,
    ContactSubmission,
    Event,
    Household,
    Rsvp,
    RsvpAnswer,
    RsvpQuestion,
)
from pages.models import WeddingSettings

RECENT_RSVP_COUNT = 20


def _progress(label: str, *, done: int, total: int) -> dict:
    """The numbers a DaisyUI `<progress>` row needs; `bar_max` avoids an invalid max="0"."""
    return {
        "label": label,
        "done": done,
        "total": total,
        "remaining": max(total - done, 0),
        "percent": round(100 * done / total) if total else 0,
        "bar_max": max(total, 1),
    }


def _by_side(done: Q) -> list[dict]:
    """Progress rows for all households, then one per side, where `done` picks the finished ones."""
    rows = {
        row["side"]: row
        for row in Household.objects.values("side")
        .annotate(total=Count("id"), done=Count("id", filter=done))
        .order_by()
    }
    result = [
        _progress(
            "All households",
            done=sum(row["done"] for row in rows.values()),
            total=sum(row["total"] for row in rows.values()),
        )
    ]
    for side, label in Household.Side.choices:
        row = rows.get(side, {"done": 0, "total": 0})
        result.append(_progress(label, done=row["done"], total=row["total"]))
    return result


def _address_section() -> dict:
    emailed = CampaignRecipient.objects.filter(household=OuterRef("pk"), status=CampaignRecipient.Status.SENT)
    missing = (
        Household.objects.filter(address_confirmed_at__isnull=True)
        .annotate(emailed=Exists(emailed))
        .prefetch_related("guests")
        .order_by("side", "name")
    )
    return {"progress": _by_side(Q(address_confirmed_at__isnull=False)), "missing": list(missing)}


def _event_section() -> list[dict]:
    """Per RSVP-required event: invited guests (household M2M × guests), yes, no, and pending."""
    events = list(Event.objects.filter(rsvp_required=True).annotate(invited=Count("households__guests")))
    counts: dict[tuple[int, bool], int] = {
        (row["event_id"], row["attending"]): row["n"]
        for row in Rsvp.objects.filter(event__in=events)
        .values("event_id", "attending")
        .annotate(n=Count("id"))
        .order_by()
    }
    section = []
    for event in events:
        yes = counts.get((event.pk, True), 0)
        no = counts.get((event.pk, False), 0)
        section.append(
            {
                "event": event,
                "invited": event.invited,
                "yes": yes,
                "no": no,
                "pending": max(event.invited - yes - no, 0),
                "progress": _progress("Responded", done=yes + no, total=event.invited),
            }
        )
    return section


def _response_section() -> dict:
    non_responders = Household.objects.filter(responded_at__isnull=True)
    nudgeable = non_responders.exclude(email="").filter(email_opt_out=False, email_bounced_at__isnull=True).count()
    total = non_responders.count()
    return {
        "progress": _by_side(Q(responded_at__isnull=False)),
        "non_responders": total,
        "nudgeable": nudgeable,
        "call": total - nudgeable,
    }


def _answer_label(answer: RsvpAnswer) -> str:
    if answer.guest_id:
        return f"{answer.guest.display_name} ({answer.household.name})"
    return answer.household.name


def _question_section() -> list[dict]:
    """Roll-ups for active `show_on_dashboard` questions: choice counts, or the non-empty text answers."""
    questions = RsvpQuestion.objects.filter(active=True, show_on_dashboard=True).prefetch_related(
        Prefetch("answers", queryset=RsvpAnswer.objects.select_related("household", "guest__household"))
    )
    section = []
    for question in questions:
        if question.is_choice or question.kind == RsvpQuestion.Kind.YES_NO:
            counter: Counter[str] = Counter()
            for answer in question.answers.all():
                value = answer.value
                if isinstance(value, bool):
                    counter["Yes" if value else "No"] += 1
                elif isinstance(value, list):
                    counter.update(str(item) for item in value if str(item).strip())
                elif value not in (None, ""):
                    counter[str(value)] += 1
            ordered = list(question.choices) if question.is_choice else ["Yes", "No"]
            labels = [*ordered, *(label for label in counter if label not in ordered)]
            total = sum(counter.values())
            section.append(
                {
                    "question": question,
                    "kind": "choices",
                    "total": total,
                    "rows": [_progress(label, done=counter.get(label, 0), total=total) for label in labels],
                }
            )
        else:
            answers = [
                {"who": _answer_label(answer), "text": text}
                for answer in question.answers.all()
                if (text := exports.answer_text(answer.value))
            ]
            section.append({"question": question, "kind": "text", "total": len(answers), "answers": answers})
    return section


def _campaigns() -> list[dict]:
    counts = {
        f"count_{status}": Count("recipients", filter=Q(recipients__status=status))
        for status in CampaignRecipient.Status.values
    }
    campaigns = Campaign.objects.annotate(**counts).order_by("-created_at")
    return [
        {
            "campaign": campaign,
            "counts": [
                (label, getattr(campaign, f"count_{status}"))
                for status, label in CampaignRecipient.Status.choices
                if getattr(campaign, f"count_{status}")
            ],
        }
        for campaign in campaigns
    ]


@staff_member_required
def dashboard(request: AppRequest) -> HttpResponse:
    wedding = WeddingSettings.load()
    new_submissions = ContactSubmission.objects.filter(status=ContactSubmission.Status.NEW).count()
    submissions_url = f"{reverse('admin:guests_contactsubmission_changelist')}?{urlencode({'status__exact': 'new'})}"
    context = {
        "address": _address_section(),
        "events": _event_section(),
        "responses": _response_section(),
        "questions": _question_section(),
        "recent_rsvps": list(
            Rsvp.objects.select_related("guest__household", "event").order_by("-responded_at", "-id")[
                :RECENT_RSVP_COUNT
            ]
        ),
        "new_submissions": new_submissions,
        "submissions_url": submissions_url,
        "bounced": list(Household.objects.filter(email_bounced_at__isnull=False).order_by("email_bounced_at")),
        "campaigns": _campaigns(),
        "has_album": bool(wedding.photo_album_url),
        "gate_enabled": wedding.gate_enabled,
    }
    return render(request, "guests/manage/dashboard.html", context)


# --- QR codes --------------------------------------------------------------------------


def site_qr_url(wedding: WeddingSettings) -> str:
    """The site link the QR encodes: the `?k=` unlock link while the gate is on, else the home page."""
    base = settings.APP_BASE_URL.rstrip("/")
    if not wedding.site_password:
        return f"{base}/"
    return f"{base}{reverse('pages:welcome')}?{urlencode({'k': wedding.site_password})}"


@staff_member_required
def qr(request: AppRequest, name: str) -> HttpResponse:
    """`site.svg` (the unlock link) and `album.svg` (the shared album). Print from the browser."""
    wedding = WeddingSettings.load()
    if name == "site":
        data = site_qr_url(wedding)
    elif name == "album" and wedding.photo_album_url:
        data = wedding.photo_album_url
    else:
        raise Http404("No such QR code.")
    image = qrcode.make(data, image_factory=SvgPathImage, box_size=10, border=2)
    return HttpResponse(image.to_string(), content_type="image/svg+xml")


# --- Exports ---------------------------------------------------------------------------


@staff_member_required
def export_rsvps(request: AppRequest) -> HttpResponse:
    events = exports.rsvp_events()
    questions = exports.active_questions()
    return exports.csv_response(
        "rsvps.csv",
        exports.rsvps_header(events, questions),
        exports.rsvps_rows(Household.objects.all(), events, questions),
    )


@staff_member_required
def export_seating(request: AppRequest) -> HttpResponse:
    seated, unassigned = exports.seating_guests(exports.reception_event())
    return exports.csv_response(
        "seating.csv",
        exports.SEATING_HEADER,
        exports.seating_rows(
            [*seated, *unassigned],
            meals=exports.answers_by_guest(exports.meal_question()),
            dietary=exports.answers_by_guest(exports.dietary_question()),
        ),
    )


@staff_member_required
def export_save_the_date_links(request: AppRequest) -> HttpResponse:
    return exports.csv_response(
        "save-the-date-links.csv",
        exports.SAVE_THE_DATE_LINKS_HEADER,
        exports.save_the_date_links_rows(Household.objects.all()),
    )


@staff_member_required
def export_invite_codes(request: AppRequest) -> HttpResponse:
    return exports.csv_response(
        "invite-codes.csv", exports.INVITE_CODES_HEADER, exports.invite_codes_rows(Household.objects.all())
    )
