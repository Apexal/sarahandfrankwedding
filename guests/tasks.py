"""Background jobs for campaigns. Autodiscovered by steady_queue like every app's tasks.py."""

import structlog
from django.tasks import task

from core.tasks import recurring_if_supported
from guests.campaigns import send_campaign_batch
from guests.models import Campaign

logger = structlog.get_logger(__name__)


@task()
def send_campaign(*, campaign_id: int) -> None:
    """Hand one daily batch of a campaign to the email queue.

    Takes an id rather than an ORM instance because that is all a queued job can carry.
    """
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if campaign is None:
        logger.warning("campaign_batch_skipped", campaign_id=campaign_id, reason="not_found")
        return
    try:
        send_campaign_batch(campaign)
    except Exception as exc:
        logger.exception(
            "campaign_batch_failed", task="send_campaign", campaign_id=campaign_id, error_type=type(exc).__name__
        )
        raise


def _resume_campaigns() -> int:
    """Enqueue a batch for every campaign still sending; return how many were enqueued."""
    campaign_ids = list(Campaign.objects.filter(status=Campaign.Status.SENDING).values_list("pk", flat=True))
    # One job per campaign: one that fails must not strand the others behind it.
    for campaign_id in campaign_ids:
        send_campaign.enqueue(campaign_id=campaign_id)
    logger.info("campaigns_resumed", queued=len(campaign_ids))
    return len(campaign_ids)


# The schedule is evaluated in the timezone steady_queue hands its cron parser; verify that
# 09:00 is local morning before relying on it (PLAN.md section 11).
# steady_queue evaluates cron in UTC: 13:00 UTC is 09:00 EDT / 08:00 EST, a local
# morning either way so the batch lands while people read email.
@recurring_if_supported(schedule="0 13 * * *", key="resume campaigns")
@task()
def resume_campaigns() -> None:
    """Each morning, send the next daily batch of every campaign that is still sending."""
    _resume_campaigns()
