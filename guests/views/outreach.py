"""Sending the save-the-date by hand, at /manage/save-the-date/, and reviewing what the generic page collected.

The save-the-date goes out three ways (PLAN.md section 10): a text from our phones with the household's
link, the `save_the_date` email campaign, and the generic link in family group chats. This page is where
we work through the texts (an `sms:` link with the message filled in, then "mark texted"), start the
email campaign, flip the two save-the-date switches, and see who has not been reached by anything yet.
Generic-page submissions are reviewed at /manage/save-the-date/submissions/ through `guests/submissions.py`.
"""

from urllib.parse import quote

import structlog
from django.contrib import messages
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from core.types import AppRequest
from guests.email_contexts import ADDRESS_NUDGE, SAVE_THE_DATE
from guests.models import Campaign, CampaignRecipient, ContactSubmission, Household
from guests.submissions import SubmissionError, create_household, merge, suggest_households
from guests.views.guest_list import staff_member_required
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)

SAVE_THE_DATE_TEMPLATES = (SAVE_THE_DATE, ADDRESS_NUDGE)

SHOW_CHOICES = [
    ("all", "All"),
    ("unreached", "Not reached yet"),
    ("to_text", "To text"),
    ("no_address", "No address yet"),
]

#: The switches this page may flip, with what each one does, in the order they are shown.
SWITCHES = {
    "save_the_date_open": (
        "Generic page open",
        "The forwardable /save-the-date/ page is live, for group chats and anyone we missed.",
    ),
    "codes_frozen": (
        "Invite codes frozen",
        "Codes can no longer be regenerated, so links already sent keep working. Turn on before the first send.",
    ),
}


def sms_href(household: Household, message: str) -> str:
    """An `sms:` link with the message filled in. `?&body=` is the form both iOS and Android read."""
    return f"sms:{household.phone}?&body={quote(message)}"


def _households():
    emailed = CampaignRecipient.objects.filter(
        household=OuterRef("pk"),
        status=CampaignRecipient.Status.SENT,
        campaign__template_key__in=SAVE_THE_DATE_TEMPLATES,
    )
    return Household.objects.annotate(emailed=Exists(emailed)).prefetch_related("guests")


def _filter(queryset, *, show: str, q: str):
    if q:
        queryset = queryset.filter(
            Q(name__icontains=q)
            | Q(invite_code__iexact=q)
            | Q(guests__first_name__icontains=q)
            | Q(guests__last_name__icontains=q)
            | Q(email__icontains=q)
        ).distinct()
    match show:
        case "unreached":
            queryset = queryset.filter(
                address_confirmed_at__isnull=True, save_the_date_texted_at__isnull=True, emailed=False
            )
        case "to_text":
            queryset = queryset.exclude(phone="").filter(save_the_date_texted_at__isnull=True)
        case "no_address":
            queryset = queryset.filter(address_confirmed_at__isnull=True)
    return queryset


def _row(household: Household, wedding: WeddingSettings) -> dict:
    message = household.save_the_date_message(wedding)
    return {
        "household": household,
        "message": message,
        "sms_href": sms_href(household, message) if household.phone else "",
        "unreachable": not household.phone and not household.can_email,
    }


def _stats() -> dict:
    households = _households()
    total = households.count()
    return {
        "total": total,
        "confirmed": households.filter(address_confirmed_at__isnull=False).count(),
        "texted": households.filter(save_the_date_texted_at__isnull=False).count(),
        "emailed": households.filter(emailed=True).count(),
        "unreached": households.filter(
            address_confirmed_at__isnull=True, save_the_date_texted_at__isnull=True, emailed=False
        ).count(),
        "to_text": households.exclude(phone="").filter(save_the_date_texted_at__isnull=True).count(),
        "can_email": households.exclude(email="").filter(email_opt_out=False, email_bounced_at__isnull=True).count(),
        "no_way": households.filter(phone="")
        .exclude(Q(email__gt="") & Q(email_opt_out=False) & Q(email_bounced_at__isnull=True))
        .count(),
    }


@staff_member_required
def save_the_date(request: AppRequest) -> HttpResponse:
    wedding = WeddingSettings.load()
    show = request.GET.get("show", "all")
    if show not in dict(SHOW_CHOICES):
        show = "all"
    q = request.GET.get("q", "").strip()
    households = _filter(_households(), show=show, q=q).order_by("sort_order", "name")
    context = {
        "rows": [_row(household, wedding) for household in households],
        "show": show,
        "show_choices": SHOW_CHOICES,
        "q": q,
    }
    if request.htmx and request.htmx.target == "std-rows":
        return render(request, "guests/manage/save_the_date/_rows.html", context)
    new_campaign_url = reverse("manage:campaign_new")
    context.update(
        {
            "nav": "save_the_date",
            "stats": _stats(),
            "switches": [
                {"name": name, "label": label, "on": getattr(wedding, name), "help": help_text}
                for name, (label, help_text) in SWITCHES.items()
            ],
            "campaigns": Campaign.objects.filter(template_key__in=SAVE_THE_DATE_TEMPLATES).order_by("-created_at"),
            "email_all_url": f"{new_campaign_url}?{urlencode({'template': SAVE_THE_DATE, 'audience': Campaign.Audience.ALL_WITH_EMAIL, 'name': 'Save the date'})}",
            "nudge_url": f"{new_campaign_url}?{urlencode({'template': ADDRESS_NUDGE, 'audience': Campaign.Audience.MISSING_ADDRESS, 'name': 'Address nudge'})}",
            "new_submissions": ContactSubmission.objects.filter(status=ContactSubmission.Status.NEW).count(),
            "generic_url": request.build_absolute_uri(reverse("save_the_date:generic")),
        }
    )
    return render(request, "guests/manage/save_the_date/page.html", context)


@staff_member_required
@require_POST
def save_the_date_switch(request: AppRequest) -> HttpResponse:
    name = request.POST.get("name", "")
    if name not in SWITCHES:
        logger.warning("save_the_date_switch_rejected", name=name)
        messages.error(request, "Unknown setting.")
        return redirect("manage:save_the_date")
    on = request.POST.get("on") == "1"
    wedding = WeddingSettings.load()
    setattr(wedding, name, on)
    wedding.save()
    logger.info("wedding_setting_changed", setting=name, value=on, user_id=request.user.pk)
    messages.success(request, f"{SWITCHES[name][0]}: {'on' if on else 'off'}.")
    return redirect("manage:save_the_date")


@staff_member_required
@require_POST
def household_texted(request: AppRequest, pk: int) -> HttpResponse:
    """Mark (or unmark) a household as texted its save-the-date link."""
    household = get_object_or_404(Household, pk=pk)
    texted = request.POST.get("texted") == "1"
    household.save_the_date_texted_at = timezone.now() if texted else None
    household.save(update_fields=("save_the_date_texted_at", "updated_at"))
    logger.info("save_the_date_texted", household_id=household.pk, texted=texted)
    if request.htmx:
        row = _row(_households().get(pk=pk), WeddingSettings.load())
        return render(request, "guests/manage/save_the_date/_row.html", {"row": row})
    return redirect(request.POST.get("next") or "manage:save_the_date")


# --- Contact submissions ---------------------------------------------------------------


@staff_member_required
def submissions(request: AppRequest) -> HttpResponse:
    status = request.GET.get("status", ContactSubmission.Status.NEW)
    if status not in ContactSubmission.Status.values:
        status = ContactSubmission.Status.NEW
    items = ContactSubmission.objects.filter(status=status).select_related("household").order_by("-created_at")
    counts = {
        value: ContactSubmission.objects.filter(status=value).count() for value in ContactSubmission.Status.values
    }
    return render(
        request,
        "guests/manage/save_the_date/submissions.html",
        {
            "nav": "save_the_date",
            "status": status,
            "tabs": [(value, label, counts[value]) for value, label in ContactSubmission.Status.choices],
            "items": [
                {
                    "submission": submission,
                    "suggestions": suggest_households(submission) if status == ContactSubmission.Status.NEW else [],
                }
                for submission in items
            ],
            "all_households": Household.objects.order_by("name").only("pk", "name")
            if status == ContactSubmission.Status.NEW
            else [],
        },
    )


def _back_to_submissions(status: str = ContactSubmission.Status.NEW) -> HttpResponse:
    return redirect(f"{reverse('manage:submissions')}?{urlencode({'status': status})}")


@staff_member_required
@require_POST
def submission_merge(request: AppRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(ContactSubmission, pk=pk)
    household = Household.objects.filter(pk=request.POST.get("household") or None).first()
    if household is None:
        messages.error(request, "Pick the household to merge into.")
        return _back_to_submissions()
    try:
        merge(submission, household)
    except SubmissionError as exc:
        logger.warning("submission_merge_rejected", submission_id=submission.pk, reason=str(exc))
        messages.error(request, str(exc))
        return _back_to_submissions()
    logger.info("submission_merged", submission_id=submission.pk, household_id=household.pk)
    messages.success(request, f"Merged “{submission.names}” into {household.name}.")
    return _back_to_submissions()


@staff_member_required
@require_POST
def submission_create(request: AppRequest, pk: int) -> HttpResponse:
    submission = get_object_or_404(ContactSubmission, pk=pk)
    try:
        household = create_household(submission)
    except SubmissionError as exc:
        logger.warning("submission_create_rejected", submission_id=submission.pk, reason=str(exc))
        messages.error(request, str(exc))
        return _back_to_submissions()
    logger.info("submission_household_created", submission_id=submission.pk, household_id=household.pk)
    messages.success(
        request,
        f"Created {household.name}. Set its side and invited events on the guest list.",
    )
    return _back_to_submissions()


@staff_member_required
@require_POST
def submission_status(request: AppRequest, pk: int) -> HttpResponse:
    """Ignore a new submission, or put an ignored one back to new."""
    submission = get_object_or_404(ContactSubmission, pk=pk)
    target = request.POST.get("status")
    allowed = {
        ContactSubmission.Status.NEW: ContactSubmission.Status.IGNORED,
        ContactSubmission.Status.IGNORED: ContactSubmission.Status.NEW,
    }
    if allowed.get(submission.status) != target:
        logger.warning(
            "submission_status_rejected", submission_id=submission.pk, status=submission.status, target=target
        )
        messages.error(request, "That submission has already been handled.")
        return _back_to_submissions(submission.status)
    previous = submission.status
    submission.status = target
    submission.save(update_fields=("status", "updated_at"))
    logger.info("submission_status_changed", submission_id=submission.pk, status=target)
    messages.success(
        request,
        f"“{submission.names}” {'ignored' if target == ContactSubmission.Status.IGNORED else 'moved back to new'}.",
    )
    return _back_to_submissions(previous)
