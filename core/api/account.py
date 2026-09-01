"""Account management API router — deletion request lifecycle.

    POST   /api/account/delete         — request deletion (starts the grace period)
    GET    /api/account/delete         — current deletion request status, or null
    POST   /api/account/delete/cancel  — cancel a pending deletion request

Both Apple and Google require an in-app account deletion path, so this ships in
the starter rather than being left to each project.
"""

import logging
from datetime import datetime

import posthog
from django.db import transaction
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from core.models import AccountDeletionRequest, User
from core.tasks import GRACE_PERIOD

logger = logging.getLogger(__name__)

router = Router(tags=["account"])


class DeletionRequestOut(Schema):
    id: int
    status: str
    scheduled_for: datetime
    completed_at: datetime | None
    is_cancelable: bool


class DeleteAccountRequest(Schema):
    reason: str = ""


@router.post("/delete", response=DeletionRequestOut)
def request_account_deletion(request, payload: DeleteAccountRequest):
    """Schedule deletion of the authenticated user's account.

    A grace period begins immediately; the user can cancel within it via
    `POST /api/account/delete/cancel`. All existing tokens are revoked on
    request, so canceling requires a fresh login — that is deliberate, it
    proves whoever cancels still controls the account.
    """
    user: User = request.auth.user

    existing = (
        AccountDeletionRequest.objects.filter(user=user, status=AccountDeletionRequest.Status.SCHEDULED)
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing

    deletion_request = AccountDeletionRequest.objects.create(
        user=user,
        status=AccountDeletionRequest.Status.SCHEDULED,
        scheduled_for=timezone.now() + GRACE_PERIOD,
        reason=payload.reason,
    )

    # Revoke every token — the account stays is_active so re-authentication (and
    # therefore cancellation) is still possible.
    now = timezone.now()
    user.access_tokens.filter(revoked_at__isnull=True).update(revoked_at=now, updated_at=now)
    user.refresh_tokens.filter(revoked_at__isnull=True).update(revoked_at=now, updated_at=now)

    posthog.capture(
        "account_deletion_requested",
        distinct_id=str(user.pk),
        properties={"grace_period_days": GRACE_PERIOD.days},
    )
    logger.info("Account deletion requested for user_id=%s, scheduled_for=%s", user.pk, deletion_request.scheduled_for)

    return deletion_request


@router.get("/delete", response=DeletionRequestOut | None)
def get_deletion_request(request):
    """Return the current pending deletion request for the authenticated user, or null."""
    return (
        AccountDeletionRequest.objects.filter(
            user=request.auth.user,
            status=AccountDeletionRequest.Status.SCHEDULED,
        )
        .order_by("-created_at")
        .first()
    )


@router.post("/delete/cancel", response=DeletionRequestOut)
def cancel_account_deletion(request):
    """Cancel a pending deletion request within the grace period.

    404 if there is no pending request; 409 if the grace period has already
    elapsed (deletion has been, or is being, processed).
    """
    user: User = request.auth.user

    with transaction.atomic():
        req = (
            AccountDeletionRequest.objects.select_for_update()
            .filter(user=user, status=AccountDeletionRequest.Status.SCHEDULED)
            .order_by("-created_at")
            .first()
        )
        if req is None:
            raise HttpError(404, "No pending deletion request found.")

        if not req.is_cancelable:
            raise HttpError(
                409,
                "The grace period has elapsed — deletion is already being processed and cannot be canceled.",
            )

        req.status = AccountDeletionRequest.Status.CANCELED
        req.save(update_fields=["status", "updated_at"])

    posthog.capture("account_deletion_canceled", distinct_id=str(user.pk))
    logger.info("Account deletion canceled for user_id=%s", user.pk)

    return req
