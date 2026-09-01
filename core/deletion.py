"""Account deletion pipeline.

The default is a hard delete of the `User` row; everything with an FK to it
cascades. That is the right default for a starter — it is unambiguous and leaves
nothing behind — but it is rarely the right default for a real product, where
deleting a user would take their orders, invoices, or clinical records with them.

Two extension points:

1. `@register_deletion_handler` — a callable run before the delete, while the
   user's fields are still populated. Use it to scrub or reassign rows in your
   own app. Import the module from your `AppConfig.ready()` so registration runs::

       # billing/apps.py
       def ready(self):
           import billing.deletion  # noqa: F401

2. Replace `_delete_user` outright if your compliance regime wants anonymization
   in place rather than deletion (keep the row, scrub every identifying field,
   and drop the audit-log snapshots that would otherwise still hold the old
   values).
"""

import logging
from collections.abc import Callable

from auditlog.context import disable_auditlog
from django.db import transaction
from django.utils import timezone

from core.models import AccountDeletionRequest, User

logger = logging.getLogger(__name__)

#: Registered per-app deletion handlers. Each receives the User *before* deletion.
#: All handlers run inside the same transaction, so a failure aborts cleanly.
DELETION_HANDLERS: list[Callable[[User], None]] = []


def register_deletion_handler(fn: Callable[[User], None]) -> Callable[[User], None]:
    """Register a callable to run against the User during account deletion."""
    DELETION_HANDLERS.append(fn)
    return fn


def _delete_user(user: User) -> None:
    user.delete()


def execute_account_deletion(user: User, *, deletion_request: AccountDeletionRequest | None = None) -> None:
    """Run every registered handler, then delete the user.

    `deletion_request` is marked completed inside the same transaction as the
    delete, so a worker that dies mid-run can't leave a request that looks
    pending against a user who is already gone.
    """
    user_id = user.pk
    # Auditlog would otherwise write a DELETE entry per cascaded row, each holding
    # a snapshot of the very fields being deleted.
    with transaction.atomic(), disable_auditlog():
        for handler in DELETION_HANDLERS:
            handler(user)

        if deletion_request is not None:
            deletion_request.status = AccountDeletionRequest.Status.COMPLETED
            deletion_request.completed_at = timezone.now()
            deletion_request.save(update_fields=["status", "completed_at", "updated_at"])

        _delete_user(user)

    logger.info("Account deletion completed for user_id=%s", user_id)
