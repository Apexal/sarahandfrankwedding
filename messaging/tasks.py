import time
from datetime import timedelta

import structlog
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.tasks import task
from django.utils import timezone

from core.tasks import recurring_if_supported
from messaging.models import OutboundEmail

logger = structlog.get_logger(__name__)


@task(queue_name="email")
def send_outbound_email_task(*, outbound_email_id: int, fail_silently: bool = False):
    # Paired with a single-threaded "email" worker, this floor caps the
    # global send rate to settings.EMAIL_RATE_LIMIT_PER_SECOND.
    started_at = time.monotonic()
    try:
        _send_outbound_email(outbound_email_id=outbound_email_id, fail_silently=fail_silently)
    finally:
        if not settings.DEBUG:
            min_duration = 1.0 / settings.EMAIL_RATE_LIMIT_PER_SECOND
            remaining = min_duration - (time.monotonic() - started_at)
            if remaining > 0:
                time.sleep(remaining)


def _send_outbound_email(*, outbound_email_id: int, fail_silently: bool = False):
    outbound_email = OutboundEmail.objects.get(pk=outbound_email_id)
    if outbound_email.status == OutboundEmail.DeliveryStatus.SENT:
        return

    to_email, subject = _apply_recipient_allowlist(outbound_email)
    message = EmailMultiAlternatives(
        subject=subject,
        body=outbound_email.body_text_rendered,
        from_email=outbound_email.from_email or None,
        to=[to_email],
        reply_to=[outbound_email.reply_to_email] if outbound_email.reply_to_email else None,
    )
    if outbound_email.body_html_rendered:
        message.attach_alternative(outbound_email.body_html_rendered, "text/html")

    _props = {"outbound_email_id": outbound_email.pk, "template_key": outbound_email.template_key}

    try:
        sent_count = message.send(fail_silently=fail_silently)
    except Exception as exc:
        outbound_email.status = OutboundEmail.DeliveryStatus.FAILED
        outbound_email.error_message = str(exc)
        outbound_email.save(update_fields=("status", "error_message", "updated_at"))
        logger.warning("email_failed", **_props, error_type=type(exc).__name__, error=str(exc))
        raise

    if sent_count > 0:
        outbound_email.status = OutboundEmail.DeliveryStatus.SENT
        outbound_email.sent_at = timezone.now()
        outbound_email.error_message = ""
        outbound_email.save(update_fields=("status", "sent_at", "error_message", "updated_at"))
        logger.info("email_sent", **_props)
        return

    outbound_email.status = OutboundEmail.DeliveryStatus.FAILED
    outbound_email.error_message = "Email backend returned zero messages sent."
    outbound_email.save(update_fields=("status", "error_message", "updated_at"))
    logger.warning("email_failed", **_props, error=outbound_email.error_message)


def _apply_recipient_allowlist(outbound_email: OutboundEmail) -> tuple[str, str]:
    """Staging safety net: return the (recipient, subject) to actually hand to the backend.

    With `EMAIL_RECIPIENT_ALLOWLIST` set, anything addressed outside it is delivered to
    the first allowed address with the original recipient in the subject, so a rehearsal
    can never reach a real guest. The `OutboundEmail` row keeps the original recipient.
    """
    allowlist = settings.EMAIL_RECIPIENT_ALLOWLIST
    to_email = outbound_email.to_email
    subject = outbound_email.subject_rendered
    if not allowlist or to_email.lower() in allowlist:
        return to_email, subject
    redirected_to = allowlist[0]
    logger.info(
        "email_recipient_redirected",
        outbound_email_id=outbound_email.pk,
        template_key=outbound_email.template_key,
        original_to=to_email,
        delivered_to=redirected_to,
    )
    return redirected_to, f"[to: {to_email}] {subject}"


def _prune_outbound_emails():
    cutoff = timezone.now() - timedelta(days=settings.OUTBOUND_EMAIL_RETENTION_DAYS)
    deleted_count, _ = OutboundEmail.objects.filter(created_at__lt=cutoff).delete()
    logger.info(
        "outbound_emails_pruned",
        deleted=deleted_count,
        retention_days=settings.OUTBOUND_EMAIL_RETENTION_DAYS,
    )
    return deleted_count


@recurring_if_supported(schedule="20 3 * * *", key="prune outbound emails")
@task()
def prune_outbound_emails():
    """Rendered email bodies can contain PII; keep only OUTBOUND_EMAIL_RETENTION_DAYS of history."""
    _prune_outbound_emails()
