from datetime import timedelta

import posthog
import structlog
from auditlog import get_logentry_model
from django.conf import settings
from django.core.management import call_command
from django.tasks import task
from django.utils import timezone
from steady_queue.recurring_task import recurring

logger = structlog.get_logger(__name__)

#: How long a user has to change their mind after requesting account deletion.
GRACE_PERIOD = timedelta(days=30)


def recurring_if_supported(*, schedule: str, key: str):
    def _decorator(task):
        if not hasattr(task, "serialize"):
            logger.debug(
                "recurring_registration_skipped",
                key=key,
                reason="task backend does not support recurring jobs",
            )
            return task
        return recurring(schedule=schedule, key=key)(task)

    return _decorator


@recurring_if_supported(schedule="* * * * *", key="worker health check")
@task()
def worker_health_check():
    print("Worker is healthy.")


def _clear_expired_sessions():
    call_command("clearsessions")
    logger.info("expired_sessions_cleared")


@recurring_if_supported(schedule="0 3 * * *", key="clear expired sessions")
@task()
def clear_expired_sessions():
    """django_session rows are never cleaned up automatically; prune daily."""
    _clear_expired_sessions()


def _prune_audit_log():
    cutoff = timezone.now() - timedelta(days=settings.AUDITLOG_RETENTION_DAYS)
    deleted_count, _ = get_logentry_model().objects.filter(timestamp__lt=cutoff).delete()
    logger.info(
        "audit_log_pruned",
        deleted=deleted_count,
        retention_days=settings.AUDITLOG_RETENTION_DAYS,
    )
    return deleted_count


@recurring_if_supported(schedule="10 3 * * *", key="prune audit log")
@task()
def prune_audit_log():
    """Keep the audit trail bounded: delete entries older than AUDITLOG_RETENTION_DAYS."""
    _prune_audit_log()


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------


@task()
def execute_account_deletion_task(*, deletion_request_id: int) -> None:
    """Run the deletion pipeline for one account.

    Takes an id rather than an ORM instance because that is all a queued job can
    carry across a process boundary.
    """
    # Imported here, not at module scope: core.deletion imports core.models, and
    # messaging.tasks imports this module, so a top-level import would make app
    # loading order matter.
    from core.deletion import execute_account_deletion
    from core.models import AccountDeletionRequest

    deletion_request = (
        AccountDeletionRequest.objects.select_related("user")
        .filter(pk=deletion_request_id, status=AccountDeletionRequest.Status.SCHEDULED)
        .first()
    )
    if deletion_request is None:
        logger.warning("account_deletion_skipped", deletion_request_id=deletion_request_id, reason="not_scheduled")
        return

    user = deletion_request.user
    if user is None:
        deletion_request.status = AccountDeletionRequest.Status.COMPLETED
        deletion_request.completed_at = timezone.now()
        deletion_request.save(update_fields=["status", "completed_at", "updated_at"])
        return

    user_id = user.pk
    with posthog.new_context():
        posthog.identify_context(str(user_id))
        execute_account_deletion(user, deletion_request=deletion_request)
        posthog.capture("account_deleted", distinct_id=str(user_id))


def _sweep_due_account_deletions() -> int:
    from core.models import AccountDeletionRequest

    due = AccountDeletionRequest.objects.filter(
        status=AccountDeletionRequest.Status.SCHEDULED,
        scheduled_for__lte=timezone.now(),
    ).values_list("pk", flat=True)

    # One job per account: a single account that fails to delete shouldn't
    # strand every other account queued behind it.
    for deletion_request_id in list(due):
        execute_account_deletion_task.enqueue(deletion_request_id=deletion_request_id)

    logger.info("account_deletions_swept", queued=len(due))
    return len(due)


@recurring_if_supported(schedule="30 3 * * *", key="sweep due account deletions")
@task()
def sweep_due_account_deletions():
    """Execute deletion requests whose grace period has elapsed."""
    _sweep_due_account_deletions()
