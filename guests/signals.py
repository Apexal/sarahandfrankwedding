"""Receivers for events from outside the request cycle. Connected by `GuestsConfig.ready()`."""

import structlog
from anymail.signals import EventType, tracking
from django.dispatch import receiver
from django.utils import timezone

from guests.models import Household

logger = structlog.get_logger(__name__)

#: Resend tracking events that mean "stop emailing this address".
BOUNCE_EVENT_TYPES = frozenset({EventType.BOUNCED, EventType.COMPLAINED})


@receiver(tracking, dispatch_uid="guests.record_email_bounce")
def record_email_bounce(sender, event, esp_name, **kwargs):
    """Flag every household at a bounced or complaining address so campaigns skip it.

    Sets `email_bounced_at` only where it is unset, so the first bounce's timestamp
    survives later ones. Cleared by hand in the admin once the address is fixed.
    """
    if event.event_type not in BOUNCE_EVENT_TYPES:
        return
    recipient = (event.recipient or "").strip()
    if not recipient:
        return

    now = timezone.now()
    households = list(Household.objects.filter(email__iexact=recipient, email_bounced_at__isnull=True))
    for household in households:
        household.email_bounced_at = now
        household.save(update_fields=("email_bounced_at", "updated_at"))
        logger.info(
            "email_bounced",
            household_id=household.pk,
            event_type=event.event_type,
            reject_reason=event.reject_reason,
            esp=esp_name,
        )
    if not households:
        logger.info("email_bounce_unmatched", event_type=event.event_type, esp=esp_name)
