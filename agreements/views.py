import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from agreements.models import Agreement, AgreementVersion, UserAgreement
from agreements.services import (
    get_client_ip,
    get_pending_versions,
    invalidate_required_version_cache,
    record_acceptance,
)
from core.types import AppRequest

logger = structlog.get_logger(__name__)


@login_required
@require_http_methods(["GET", "POST"])
def pending_agreements(request: AppRequest):
    """Show every required agreement the user still owes and record acceptance.

    All pending agreements are accepted in one submission — the user must tick each
    one individually, so consent stays per-agreement even though the form is single.
    """
    versions = get_pending_versions(request.user)
    if not versions:
        # The gate sent the user here on the strength of the cached required-version
        # list, so if there is nothing to render that list disagrees with the database
        # (e.g. it names a version that no longer exists). Drop it before bouncing
        # home, or the gate redirects straight back here and the user is stuck in a
        # loop until the cache expires.
        invalidate_required_version_cache()
        return redirect("core:home")

    if request.method == "POST":
        confirmed = set(request.POST.getlist("accept"))
        unconfirmed = [version for version in versions if str(version.pk) not in confirmed]
        if unconfirmed:
            messages.error(request, "Please confirm each agreement to continue.")
        else:
            ip_address = get_client_ip(request)
            user_agent = request.META.get("HTTP_USER_AGENT", "")
            with transaction.atomic():
                for version in versions:
                    record_acceptance(request.user, version, ip_address=ip_address, user_agent=user_agent)
            messages.success(request, "Thank you — your agreements are up to date.")
            return redirect("core:home")

    return render(request, "agreements/pending.html", {"versions": versions})


@login_required
def agreement_history(request: AppRequest):
    """The signed-in user's own acceptance record."""
    acceptances = (
        UserAgreement.objects.filter(user=request.user)
        .select_related("agreement_version__agreement")
        .order_by("-created_at")
    )
    return render(request, "agreements/history.html", {"acceptances": acceptances})


@login_required
def version_detail(request: AppRequest, version_id: int):
    """A specific historical version, as the user saw it when they accepted.

    Scoped to versions that have actually been published: unpublished drafts are the
    next set of terms, and IDs here are sequential and guessable, so an unscoped
    lookup would let any signed-in user read them before they are announced.
    """
    version = get_object_or_404(
        AgreementVersion.objects.select_related("agreement").filter(published_at__isnull=False),
        pk=version_id,
    )
    accepted = UserAgreement.objects.filter(user=request.user, agreement_version=version).first()
    return render(
        request,
        "agreements/detail.html",
        {"version": version, "accepted_at": accepted.created_at if accepted else None},
    )


def current_agreement(request: AppRequest, key: str):
    """The current published version of an agreement, readable without signing in.

    This is what footer links to "Terms of Service" or "Privacy Policy" should point at.
    """
    agreement = get_object_or_404(Agreement.objects.select_related("current_version__agreement"), key=key)
    if agreement.current_version is None:
        raise Http404("This agreement has no published version yet.")

    accepted_at = None
    if request.user.is_authenticated:
        accepted = UserAgreement.objects.filter(user=request.user, agreement_version=agreement.current_version).first()
        accepted_at = accepted.created_at if accepted else None

    return render(
        request,
        "agreements/detail.html",
        {"version": agreement.current_version, "accepted_at": accepted_at},
    )
