"""Read/write helpers shared by the gate middleware, the views, and the admin.

Everything that answers "what does this user still owe us?" lives here so the
middleware and the pending page can never disagree about what counts as pending.
"""

import posthog
import structlog
from django.core.cache import cache
from django.db import transaction

from agreements.models import Agreement, AgreementVersion, UserAgreement

logger = structlog.get_logger(__name__)

# The gate runs on every request from every signed-in user, so the "which versions
# are required right now?" half of the check is cached. It is a short list of ints
# that only changes when staff publish, so it is invalidated by the receivers in
# agreements/signals.py rather than left to expire.
REQUIRED_VERSIONS_CACHE_KEY = "agreements:required_version_ids"
REQUIRED_VERSIONS_CACHE_TIMEOUT = 60 * 60  # safety net only; publishes invalidate explicitly

# Enough to identify a browser without storing an unbounded blob per acceptance.
USER_AGENT_MAX_LENGTH = 500


def get_required_version_ids() -> list[int]:
    """IDs of the AgreementVersions every user must currently have accepted."""
    version_ids = cache.get(REQUIRED_VERSIONS_CACHE_KEY)
    if version_ids is None:
        version_ids = list(
            Agreement.objects.filter(
                required=True,
                requires_reacceptance=True,
                current_version__isnull=False,
            ).values_list("current_version_id", flat=True)
        )
        cache.set(REQUIRED_VERSIONS_CACHE_KEY, version_ids, REQUIRED_VERSIONS_CACHE_TIMEOUT)
    return version_ids


def invalidate_required_version_cache() -> None:
    cache.delete(REQUIRED_VERSIONS_CACHE_KEY)


def has_pending_agreements(user) -> bool:
    """Whether `user` is missing any currently-required agreement.

    One query in the worst case, zero when nothing is required — this is the hot
    path called by AgreementGateMiddleware on every request.
    """
    required_ids = get_required_version_ids()
    if not required_ids:
        return False
    accepted_count = UserAgreement.objects.filter(user=user, agreement_version_id__in=required_ids).count()
    return accepted_count < len(required_ids)


def get_pending_versions(user) -> list[AgreementVersion]:
    """The required AgreementVersions `user` has not accepted, ready to render."""
    required_ids = get_required_version_ids()
    if not required_ids:
        return []
    accepted_ids = set(
        UserAgreement.objects.filter(user=user, agreement_version_id__in=required_ids).values_list(
            "agreement_version_id", flat=True
        )
    )
    pending_ids = [version_id for version_id in required_ids if version_id not in accepted_ids]
    if not pending_ids:
        return []
    return list(
        AgreementVersion.objects.filter(pk__in=pending_ids).select_related("agreement").order_by("agreement__name")
    )


def record_acceptance(user, version: AgreementVersion, *, ip_address=None, user_agent="") -> UserAgreement:
    """Record `user` accepting `version`. Idempotent — re-accepting keeps the first record."""
    user_agreement, created = UserAgreement.objects.get_or_create(
        user=user,
        agreement_version=version,
        defaults={
            "ip_address": ip_address,
            "user_agent": (user_agent or "")[:USER_AGENT_MAX_LENGTH],
        },
    )
    if created:
        # Deferred to commit: the caller accepts several agreements in one atomic
        # block, so emitting here would leave the logs and PostHog asserting consent
        # that a later failure in that block rolled back.
        transaction.on_commit(lambda: _report_acceptance(user, version))
    return user_agreement


def _report_acceptance(user, version: AgreementVersion) -> None:
    logger.info(
        "agreement_accepted",
        user_id=user.pk,
        agreement_key=version.agreement.key,
        agreement_version=version.version,
    )
    posthog.capture(
        "agreement_accepted",
        distinct_id=str(user.pk),
        properties={
            "agreement_key": version.agreement.key,
            "agreement_version": version.version,
            "agreement_version_id": version.pk,
        },
    )


def get_client_ip(request) -> str | None:
    """The requesting client's IP.

    REMOTE_ADDR is authoritative here: TrustedClientIPMiddleware (first in
    MIDDLEWARE) has already replaced it with the trusted proxy header when the
    app runs behind one. Reading X-Forwarded-For directly would accept a
    spoofed value in any deployment that is not behind such a proxy.
    """
    return request.META.get("REMOTE_ADDR") or None
