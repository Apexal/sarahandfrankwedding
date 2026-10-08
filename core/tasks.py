import io

import structlog
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.tasks import task
from django.utils import timezone
from steady_queue.recurring_task import recurring

logger = structlog.get_logger(__name__)


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


#: Nightly guest-data dumps kept in the bucket. The guest list is the crown jewel.
GUEST_BACKUP_KEEP = 30
GUEST_BACKUP_PREFIX = "backups/"


def _backup_guest_data(*, keep: int = GUEST_BACKUP_KEEP) -> str:
    """Write `dumpdata guests pages` to the default storage and keep the newest `keep` files.

    Restore with `manage.py loaddata <file>`. Fly Postgres snapshots sit underneath this;
    their retention is short, which is why the JSON copy exists.
    """
    buffer = io.StringIO()
    call_command("dumpdata", "guests", "pages", format="json", indent=2, stdout=buffer)
    name = f"{GUEST_BACKUP_PREFIX}guests-{timezone.localdate():%Y-%m-%d}.json"
    if default_storage.exists(name):
        default_storage.delete(name)
    saved_name = default_storage.save(name, ContentFile(buffer.getvalue().encode()))
    removed = _prune_guest_backups(keep=keep)
    logger.info("guest_data_backed_up", file=saved_name, bytes=buffer.tell(), pruned=removed)
    return saved_name


def _prune_guest_backups(*, keep: int) -> int:
    try:
        _, files = default_storage.listdir(GUEST_BACKUP_PREFIX)
    except FileNotFoundError, NotADirectoryError:
        return 0
    backups = sorted(name for name in files if name.startswith("guests-") and name.endswith(".json"))
    stale = backups[:-keep] if keep > 0 else backups
    for name in stale:
        default_storage.delete(f"{GUEST_BACKUP_PREFIX}{name}")
    return len(stale)


# Cron is evaluated in UTC by steady_queue: 03:40 UTC is late evening Eastern, after the
# day's RSVPs and before the morning campaign batch.
@recurring_if_supported(schedule="40 3 * * *", key="backup guest data")
@task()
def backup_guest_data():
    try:
        _backup_guest_data()
    except Exception as exc:
        logger.exception("guest_data_backup_failed", task="backup_guest_data", error_type=type(exc).__name__)
        raise
