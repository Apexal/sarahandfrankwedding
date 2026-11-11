"""Email campaigns at /manage/campaigns/ and the email templates at /manage/emails/.

A campaign is drafted here (template, audience, daily cap), previewed against a real household in its
audience, test-sent to whoever is signed in, then sent. Sending is `guests.campaigns.start_campaign`:
the first batch goes to the email queue at once and `resume_campaigns` sends one batch each morning
after; "Send next batch now" skips the wait. The detail page then tracks every recipient through to
the provider (its `OutboundEmail` status). Template bodies are the `EmailTemplateVersion` rows, edited
at /manage/emails/<key>/ with a live preview rendered by `guests/email_preview.py`.
"""

import math

import structlog
from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.types import AppRequest
from guests import email_preview
from guests.campaigns import (
    CAMPAIGN_CONTEXT_BUILDERS,
    CAMPAIGN_TEMPLATE_HELP,
    CampaignError,
    retry_failed,
    send_next_batch_now,
    start_campaign,
)
from guests.forms.campaigns import CampaignForm, DailyCapForm, EmailTemplateForm, campaign_template_choices
from guests.models import Campaign, CampaignRecipient, Household
from guests.views.guest_list import staff_member_required
from messaging.models import EmailTemplateVersion, OutboundEmail
from messaging.template_definitions import (
    get_allowed_template_keys,
    get_context_variable_names,
    get_template_definition,
)

logger = structlog.get_logger(__name__)

AUDIENCE_PREVIEW_LIMIT = 200


def _status_counts(campaign: Campaign) -> dict[str, int]:
    rows = campaign.recipients.values("status").annotate(n=Count("id")).order_by()
    counts = {status: 0 for status in CampaignRecipient.Status.values}
    counts.update({row["status"]: row["n"] for row in rows})
    delivery = OutboundEmail.objects.filter(
        pk__in=campaign.recipients.filter(status=CampaignRecipient.Status.SENT).values("outbound_email")
    )
    counts["delivered"] = delivery.filter(status=OutboundEmail.DeliveryStatus.SENT).count()
    counts["delivery_failed"] = delivery.filter(status=OutboundEmail.DeliveryStatus.FAILED).count()
    counts["in_flight"] = delivery.filter(status=OutboundEmail.DeliveryStatus.QUEUED).count()
    return counts


def _load(pk: int) -> Campaign:
    return get_object_or_404(Campaign.objects.select_related("event", "created_by"), pk=pk)


def _template_label(key: str) -> str:
    definition = get_template_definition(key)
    return definition.name if definition else key or "—"


# --- List -------------------------------------------------------------------------------


@staff_member_required
def campaigns(request: AppRequest) -> HttpResponse:
    counts = {
        f"count_{status}": Count("recipients", filter=Q(recipients__status=status), distinct=True)
        for status in CampaignRecipient.Status.values
    }
    rows = [
        {
            "campaign": campaign,
            "template": _template_label(campaign.template_key),
            "counts": [
                (label, getattr(campaign, f"count_{status}"))
                for status, label in CampaignRecipient.Status.choices
                if getattr(campaign, f"count_{status}")
            ],
            "audience_size": campaign.audience_households().count()
            if campaign.status == Campaign.Status.DRAFT
            else None,
        }
        for campaign in Campaign.objects.annotate(**counts).order_by("-created_at")
    ]
    return render(request, "guests/manage/campaigns/list.html", {"nav": "campaigns", "rows": rows})


# --- Create and edit --------------------------------------------------------------------


def _form_page(request, form: CampaignForm, campaign: Campaign | None) -> HttpResponse:
    return render(
        request,
        "guests/manage/campaigns/form.html",
        {
            "nav": "campaigns",
            "form": form,
            "campaign": campaign,
            "template_help": [
                (label, CAMPAIGN_TEMPLATE_HELP.get(key, "")) for key, label in campaign_template_choices()
            ],
        },
    )


@staff_member_required
def campaign_new(request: AppRequest) -> HttpResponse:
    if request.method == "POST":
        form = CampaignForm(request.POST)
        if form.is_valid():
            campaign = form.save(commit=False)
            campaign.created_by = request.user
            campaign.save()
            form.save_m2m()
            logger.info("campaign_created", campaign_id=campaign.pk, template_key=campaign.template_key)
            return redirect("manage:campaign_detail", pk=campaign.pk)
    else:
        initial = {"daily_cap": Campaign._meta.get_field("daily_cap").default}
        template = request.GET.get("template", "")
        if template in CAMPAIGN_CONTEXT_BUILDERS:
            initial["template_key"] = template
        audience = request.GET.get("audience", "")
        if audience in Campaign.Audience.values:
            initial["audience"] = audience
        if request.GET.get("name"):
            initial["name"] = request.GET["name"][:200]
        household_ids = [value for value in request.GET.getlist("household") if value.isdigit()]
        if household_ids:
            initial["audience"] = Campaign.Audience.SELECTED
            initial["households"] = household_ids
        form = CampaignForm(initial=initial)
    return _form_page(request, form, None)


def _draft_or_redirect(request, campaign: Campaign) -> HttpResponse | None:
    if campaign.status != Campaign.Status.DRAFT:
        messages.error(request, "Only a draft can be changed. This one has already started sending.")
        return redirect("manage:campaign_detail", pk=campaign.pk)
    return None


@staff_member_required
def campaign_edit(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    if response := _draft_or_redirect(request, campaign):
        return response
    if request.method == "POST":
        form = CampaignForm(request.POST, instance=campaign)
        if form.is_valid():
            form.save()
            logger.info("campaign_updated", campaign_id=campaign.pk)
            return redirect("manage:campaign_detail", pk=campaign.pk)
    else:
        form = CampaignForm(instance=campaign)
    return _form_page(request, form, campaign)


@staff_member_required
@require_POST
def campaign_delete(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    if response := _draft_or_redirect(request, campaign):
        return response
    name = campaign.name
    campaign.delete()
    logger.info("campaign_deleted", campaign_id=pk)
    messages.success(request, f"Deleted the draft “{name}”.")
    return redirect("manage:campaigns")


# --- Detail -----------------------------------------------------------------------------


def _preview_household(request, campaign: Campaign, audience: list[Household]) -> Household | None:
    requested = request.GET.get("household", "")
    if requested.isdigit():
        household = Household.objects.prefetch_related("guests").filter(pk=requested).first()
        if household is not None:
            return household
    return audience[0] if audience else None


@staff_member_required
def campaign_detail(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    is_draft = campaign.status == Campaign.Status.DRAFT
    context: dict = {
        "nav": "campaigns",
        "campaign": campaign,
        "template_label": _template_label(campaign.template_key),
        "is_draft": is_draft,
        "cap_form": DailyCapForm(instance=campaign),
    }

    if is_draft:
        audience = list(campaign.audience_households().prefetch_related("guests").order_by("name"))
        skipped = []
        if campaign.audience == Campaign.Audience.SELECTED:
            audience_ids = {household.pk for household in audience}
            skipped = list(campaign.households.exclude(pk__in=audience_ids).order_by("name"))
        context.update(
            {
                "audience": audience[:AUDIENCE_PREVIEW_LIMIT],
                "audience_size": len(audience),
                "audience_truncated": len(audience) > AUDIENCE_PREVIEW_LIMIT,
                "skipped": skipped,
                "days": math.ceil(len(audience) / campaign.daily_cap) if audience else 0,
            }
        )
        preview_pool = audience
    else:
        recipients = list(
            campaign.recipients.select_related("household", "outbound_email").order_by("status", "household__name")
        )
        counts = _status_counts(campaign)
        context.update(
            {
                "recipients": recipients,
                "counts": counts,
                "days_left": math.ceil(counts[CampaignRecipient.Status.QUEUED] / campaign.daily_cap),
                "has_failures": counts[CampaignRecipient.Status.FAILED] or counts["delivery_failed"],
            }
        )
        preview_pool = [recipient.household for recipient in recipients]

    household = _preview_household(request, campaign, preview_pool)
    if campaign.template_key:
        context["preview"] = email_preview.render_stored(campaign.template_key, household, campaign=campaign)
    context["preview_household"] = household
    context["preview_choices"] = preview_pool[:AUDIENCE_PREVIEW_LIMIT]
    return render(request, "guests/manage/campaigns/detail.html", context)


# --- Actions ----------------------------------------------------------------------------


@staff_member_required
@require_POST
def campaign_send(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    if request.POST.get("confirm") != "1":
        messages.error(request, "Tick the box to confirm, then press Send.")
        return redirect("manage:campaign_detail", pk=pk)
    try:
        start_campaign(campaign)
    except CampaignError as exc:
        logger.warning("campaign_send_rejected", campaign_id=pk, reason=str(exc))
        messages.error(request, str(exc))
        return redirect("manage:campaign_detail", pk=pk)
    messages.success(
        request,
        f"Sending. The first {campaign.daily_cap} are on their way; the rest go out each morning, "
        "or press “Send next batch now”.",
    )
    return redirect("manage:campaign_detail", pk=pk)


@staff_member_required
@require_POST
def campaign_next_batch(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    try:
        send_next_batch_now(campaign)
    except CampaignError as exc:
        logger.warning("campaign_next_batch_rejected", campaign_id=pk, reason=str(exc))
        messages.error(request, str(exc))
    else:
        messages.success(request, f"The next {campaign.daily_cap} are on their way.")
    return redirect("manage:campaign_detail", pk=pk)


@staff_member_required
@require_POST
def campaign_retry(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    result = retry_failed(campaign)
    total = result["recipients"] + result["emails"]
    if total:
        messages.success(request, f"Trying {total} failed email{'s' if total != 1 else ''} again.")
    else:
        messages.info(request, "Nothing has failed.")
    return redirect("manage:campaign_detail", pk=pk)


@staff_member_required
@require_POST
def campaign_cap(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    form = DailyCapForm(request.POST, instance=campaign)
    if form.is_valid():
        form.save()
        logger.info("campaign_daily_cap_changed", campaign_id=pk, daily_cap=campaign.daily_cap)
        messages.success(request, f"Now sending {campaign.daily_cap} per day.")
    else:
        messages.error(request, "Emails per day must be a whole number of at least 1.")
    return redirect("manage:campaign_detail", pk=pk)


@staff_member_required
@require_POST
def campaign_test(request: AppRequest, pk: int) -> HttpResponse:
    campaign = _load(pk)
    if not campaign.template_key:
        messages.error(request, "Pick an email for this campaign first.")
        return redirect("manage:campaign_detail", pk=pk)
    household = Household.objects.prefetch_related("guests").filter(pk=request.POST.get("household") or None).first()
    try:
        email_preview.send_test(campaign.template_key, request.user.email, household, campaign=campaign)
    except email_preview.PreviewError as exc:
        logger.warning("campaign_test_rejected", campaign_id=pk, reason=str(exc))
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Test sent to {request.user.email}.")
    url = f"{_detail_url(pk)}?household={household.pk}" if household else _detail_url(pk)
    return redirect(url)


def _detail_url(pk: int) -> str:
    return reverse("manage:campaign_detail", args=[pk])


# --- Email templates --------------------------------------------------------------------


def _definition_or_404(key: str):
    definition = get_template_definition(key)
    if definition is None:
        raise Http404("No such email template.")
    return definition


def _sample_households() -> list[Household]:
    return list(Household.objects.prefetch_related("guests").order_by("sort_order", "name")[:AUDIENCE_PREVIEW_LIMIT])


def _household_from(value: str | None) -> Household | None:
    if not value or not str(value).isdigit():
        return None
    return Household.objects.prefetch_related("guests").filter(pk=value).first()


@staff_member_required
def email_templates(request: AppRequest) -> HttpResponse:
    stored = {row.template_key: row for row in EmailTemplateVersion.objects.all()}
    rows = []
    for key in get_allowed_template_keys():
        definition = get_template_definition(key)
        rows.append(
            {
                "key": key,
                "name": definition.name if definition else key,
                "stored": stored.get(key),
                "campaign": key in CAMPAIGN_CONTEXT_BUILDERS,
                "help": CAMPAIGN_TEMPLATE_HELP.get(key, "Sent automatically by the RSVP flow."),
            }
        )
    return render(request, "guests/manage/emails/list.html", {"nav": "email_templates", "rows": rows})


@staff_member_required
def email_template(request: AppRequest, key: str) -> HttpResponse:
    definition = _definition_or_404(key)
    instance = email_preview.stored_template(key) or EmailTemplateVersion(template_key=key)
    if request.method == "POST":
        form = EmailTemplateForm(request.POST, instance=instance)
        if form.is_valid():
            preview = email_preview.render(
                key,
                form.cleaned_data["subject_template"],
                form.cleaned_data["html_body_template"],
                email_preview.context_object(key, None),
            )
            if preview["error"]:
                form.add_error("html_body_template", preview["error"])
            else:
                form.save()
                logger.info("email_template_saved", template_key=key, user_id=request.user.pk)
                messages.success(request, f"Saved “{definition.name}”.")
                return redirect("manage:email_template", key=key)
    else:
        form = EmailTemplateForm(instance=instance)

    household = _household_from(request.GET.get("household")) if key in CAMPAIGN_CONTEXT_BUILDERS else None
    households = _sample_households() if key in CAMPAIGN_CONTEXT_BUILDERS else []
    if household is None and households:
        household = households[0]
    preview = email_preview.render(
        key,
        form["subject_template"].value() or "",
        form["html_body_template"].value() or "",
        email_preview.context_object(key, household),
    )
    return render(
        request,
        "guests/manage/emails/edit.html",
        {
            "nav": "email_templates",
            "key": key,
            "definition": definition,
            "form": form,
            "saved": instance.pk is not None,
            "variables": get_context_variable_names(key),
            "help": CAMPAIGN_TEMPLATE_HELP.get(key, "Sent automatically by the RSVP flow."),
            "households": households,
            "preview_household": household,
            "preview": preview,
        },
    )


@staff_member_required
@require_POST
def email_template_preview(request: AppRequest, key: str) -> HttpResponse:
    """The live preview beside the editor: renders the unsaved subject and body as typed."""
    _definition_or_404(key)
    household = _household_from(request.POST.get("household")) if key in CAMPAIGN_CONTEXT_BUILDERS else None
    preview = email_preview.render(
        key,
        request.POST.get("subject_template", ""),
        request.POST.get("html_body_template", ""),
        email_preview.context_object(key, household),
    )
    return render(request, "guests/manage/emails/_preview.html", {"preview": preview})


@staff_member_required
@require_POST
def email_template_test(request: AppRequest, key: str) -> HttpResponse:
    _definition_or_404(key)
    household = _household_from(request.POST.get("household")) if key in CAMPAIGN_CONTEXT_BUILDERS else None
    try:
        email_preview.send_test(key, request.user.email, household)
    except email_preview.PreviewError as exc:
        logger.warning("email_template_test_rejected", template_key=key, reason=str(exc))
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Test of the saved version sent to {request.user.email}.")
    return redirect("manage:email_template", key=key)
