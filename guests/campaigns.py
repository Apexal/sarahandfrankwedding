"""Campaign sending: turn a `Campaign` into `CampaignRecipient` rows and hand them to the email queue.

PLAN.md section 11. "Send" (`start_campaign`) flips the campaign to `sending` and enqueues
`guests.tasks.send_campaign`; each run of `send_campaign_batch` hands at most `daily_cap`
emails to the `email` queue, and the nightly `resume_campaigns` task keeps calling it until
nobody is left. A recipient's `sent` means handed to the queue: delivery status lives on
its `OutboundEmail`.
"""

from collections.abc import Callable

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.html import strip_tags

from guests.email_contexts import (
    ADDRESS_NUDGE,
    ANNOUNCEMENT,
    RSVP_NUDGE,
    SAVE_THE_DATE,
    build_address_nudge_context,
    build_announcement_context,
    build_rsvp_nudge_context,
    build_save_the_date_context,
)
from guests.models import Campaign, CampaignRecipient, Household
from messaging.models import OutboundEmail
from messaging.services import EmailTemplateService
from messaging.tasks import send_outbound_email_task
from pages.models import WeddingSettings

logger = structlog.get_logger(__name__)


class CampaignError(Exception):
    """A campaign cannot be started as it stands; the message is fit to show an admin."""


#: Templates a campaign can send, and how to build each one's context for a household.
#: Confirmation and internal-notice templates are sent by the RSVP flow, not by campaigns.
CAMPAIGN_CONTEXT_BUILDERS: dict[str, Callable[[Household, WeddingSettings, Campaign | None], object]] = {
    SAVE_THE_DATE: lambda household, wedding, campaign: build_save_the_date_context(household, wedding),
    ADDRESS_NUDGE: lambda household, wedding, campaign: build_address_nudge_context(household, wedding),
    RSVP_NUDGE: lambda household, wedding, campaign: build_rsvp_nudge_context(household, wedding),
    ANNOUNCEMENT: lambda household, wedding, campaign: build_announcement_context(
        household, wedding, body_html=campaign.message if campaign else ""
    ),
}

#: What each campaign template is for, shown beside the template picker on /manage/campaigns/.
CAMPAIGN_TEMPLATE_HELP = {
    SAVE_THE_DATE: "The save-the-date itself, with their personal link to confirm their address.",
    ADDRESS_NUDGE: "A reminder to confirm their address. Pair it with “Address not yet confirmed”.",
    RSVP_NUDGE: "A reminder to RSVP, with their invite code. Pair it with “Has not RSVPed”.",
    ANNOUNCEMENT: "A message you write below (hotel block, schedule news). Fill in the message.",
}


def build_campaign_context(campaign: Campaign, household: Household, wedding: WeddingSettings) -> object:
    try:
        builder = CAMPAIGN_CONTEXT_BUILDERS[campaign.template_key]
    except KeyError:
        raise CampaignError(f"'{campaign.template_key}' is not a template a campaign can send.") from None
    return builder(household, wedding, campaign)


def start_campaign(campaign: Campaign) -> None:
    """Move a draft to `sending` and enqueue its first batch. Raises `CampaignError` if it cannot."""
    if campaign.status != Campaign.Status.DRAFT:
        raise CampaignError(f"Campaign is {campaign.get_status_display().lower()}, not a draft.")
    if not campaign.template_key:
        raise CampaignError("Pick a template before sending.")
    if campaign.template_key not in CAMPAIGN_CONTEXT_BUILDERS:
        raise CampaignError(f"'{campaign.template_key}' is not a template a campaign can send.")
    if campaign.template_key == ANNOUNCEMENT and not strip_tags(campaign.message).strip():
        raise CampaignError("Write the announcement's message before sending.")
    if campaign.audience == Campaign.Audience.ATTENDING and not campaign.event_id:
        raise CampaignError("Pick the event for an “attending an event” audience.")

    # Imported here: guests.tasks imports this module for the batch function.
    from guests.tasks import send_campaign

    campaign.status = Campaign.Status.SENDING
    campaign.save(update_fields=("status", "updated_at"))
    send_campaign.enqueue(campaign_id=campaign.pk)
    logger.info(
        "campaign_started",
        campaign_id=campaign.pk,
        template_key=campaign.template_key,
        audience=campaign.audience,
        daily_cap=campaign.daily_cap,
    )


def send_next_batch_now(campaign: Campaign) -> None:
    """Enqueue the next batch of a sending campaign today instead of waiting for tomorrow morning."""
    if campaign.status != Campaign.Status.SENDING:
        raise CampaignError(f"Campaign is {campaign.get_status_display().lower()}, not sending.")
    from guests.tasks import send_campaign

    send_campaign.enqueue(campaign_id=campaign.pk)
    logger.info("campaign_batch_requested", campaign_id=campaign.pk, daily_cap=campaign.daily_cap)


def retry_failed(campaign: Campaign) -> dict[str, int]:
    """Try every failure again: recipients that never rendered go back in the queue (reopening a
    finished campaign), and emails the provider refused are handed to the email worker again."""
    failed_recipients = campaign.recipients.filter(status=CampaignRecipient.Status.FAILED)
    requeued = failed_recipients.update(status=CampaignRecipient.Status.QUEUED, error="")
    failed_emails = list(
        OutboundEmail.objects.filter(
            pk__in=campaign.recipients.filter(status=CampaignRecipient.Status.SENT).values("outbound_email"),
            status=OutboundEmail.DeliveryStatus.FAILED,
        )
    )
    for outbound_email in failed_emails:
        outbound_email.status = OutboundEmail.DeliveryStatus.QUEUED
        outbound_email.error_message = ""
        outbound_email.save(update_fields=("status", "error_message", "updated_at"))
        send_outbound_email_task.enqueue(outbound_email_id=outbound_email.pk)
    if requeued:
        if campaign.status == Campaign.Status.SENT:
            campaign.status = Campaign.Status.SENDING
            campaign.sent_at = None
            campaign.save(update_fields=("status", "sent_at", "updated_at"))
        send_next_batch_now(campaign)
    logger.info("campaign_failures_retried", campaign_id=campaign.pk, recipients=requeued, emails=len(failed_emails))
    return {"recipients": requeued, "emails": len(failed_emails)}


def send_campaign_batch(campaign: Campaign) -> int:
    """Hand up to `daily_cap` queued recipients to the email queue; return how many were handed off.

    Creates the recipient rows on the first run. Marks the campaign `sent` once no queued
    recipient remains. Safe to run again: a finished campaign does nothing.
    """
    if campaign.status != Campaign.Status.SENDING:
        logger.warning("campaign_batch_skipped", campaign_id=campaign.pk, status=campaign.status)
        return 0

    _ensure_recipients(campaign)
    wedding = WeddingSettings.load()
    service = EmailTemplateService()
    batch = list(
        campaign.recipients.filter(status=CampaignRecipient.Status.QUEUED)
        .select_related("household")
        .order_by("household__name", "pk")[: campaign.daily_cap]
    )

    handed_off = 0
    for recipient in batch:
        if _send_to_recipient(campaign, recipient, wedding=wedding, service=service):
            handed_off += 1

    remaining = campaign.recipients.filter(status=CampaignRecipient.Status.QUEUED).count()
    logger.info("campaign_batch_sent", campaign_id=campaign.pk, handed_off=handed_off, remaining=remaining)
    if remaining == 0:
        _finish_campaign(campaign)
    return handed_off


def _ensure_recipients(campaign: Campaign) -> None:
    """Freeze the audience into recipient rows the first time a campaign runs.

    For the `selected` audience, households the audience definition excludes are recorded
    as `skipped` with the reason, so the admin can see why someone on the list got nothing.
    """
    if campaign.recipients.exists():
        return
    audience = list(campaign.audience_households())
    recipients = [CampaignRecipient(campaign=campaign, household=household) for household in audience]
    if campaign.audience == Campaign.Audience.SELECTED:
        audience_ids = {household.pk for household in audience}
        for household in campaign.households.exclude(pk__in=audience_ids):
            recipients.append(
                CampaignRecipient(
                    campaign=campaign,
                    household=household,
                    status=CampaignRecipient.Status.SKIPPED,
                    error=_skip_reason(household),
                )
            )
    with transaction.atomic():
        CampaignRecipient.objects.bulk_create(recipients)
    logger.info(
        "campaign_recipients_created",
        campaign_id=campaign.pk,
        queued=len(audience),
        skipped=len(recipients) - len(audience),
    )


def _skip_reason(household: Household) -> str:
    if not household.email:
        return "No email address."
    if household.email_opt_out:
        return "Opted out of email."
    if household.email_bounced_at is not None:
        return f"Email bounced on {household.email_bounced_at:%Y-%m-%d}."
    return "Not in the audience."  # pragma: no cover - audience_households() excludes only the three above


def _send_to_recipient(
    campaign: Campaign,
    recipient: CampaignRecipient,
    *,
    wedding: WeddingSettings,
    service: EmailTemplateService,
) -> bool:
    household = recipient.household
    try:
        context = build_campaign_context(campaign, household, wedding)
        outbound_email = service.queue_email(
            template_key=campaign.template_key,
            to_email=household.email,
            context_data=context,
            reply_to=wedding.contact_email or None,
        )
    except Exception as exc:
        recipient.status = CampaignRecipient.Status.FAILED
        recipient.error = str(exc)
        recipient.save(update_fields=("status", "error", "updated_at"))
        logger.exception(
            "campaign_recipient_failed",
            campaign_id=campaign.pk,
            template_key=campaign.template_key,
            household_id=household.pk,
            error_type=type(exc).__name__,
        )
        return False

    recipient.outbound_email = outbound_email
    recipient.status = CampaignRecipient.Status.SENT
    recipient.error = ""
    recipient.save(update_fields=("outbound_email", "status", "error", "updated_at"))
    return True


def _finish_campaign(campaign: Campaign) -> None:
    counts = {status: campaign.recipients.filter(status=status).count() for status in CampaignRecipient.Status.values}
    campaign.status = Campaign.Status.SENT
    campaign.sent_at = timezone.now()
    campaign.save(update_fields=("status", "sent_at", "updated_at"))
    logger.info(
        "campaign_sent",
        campaign_id=campaign.pk,
        template_key=campaign.template_key,
        audience=campaign.audience,
        daily_cap=campaign.daily_cap,
        **counts,
    )
