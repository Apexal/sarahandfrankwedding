import posthog
import structlog
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import BACKEND_SESSION_KEY, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from core.constants import SESSION_IMPERSONATOR_USER_ID_KEY
from core.forms import UserProfileForm
from core.models import User
from core.types import AppRequest

logger = structlog.get_logger(__name__)


def home(request: AppRequest):
    return render(request, "core/index.html")


def health(request: AppRequest):
    """Health check with database connectivity verification."""
    try:
        connection.ensure_connection()
        return JsonResponse({"status": "ok", "database": "connected"})
    except Exception as exc:
        # Don't echo exception details to unauthenticated callers — report
        # them to PostHog and the logs instead.
        posthog.capture_exception(
            exc,
            distinct_id="system",
            properties={"handler": "health", "error_type": type(exc).__name__},
        )
        logger.error("health_check_failed", error=str(exc))
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)


@require_GET
@cache_control(max_age=86400, public=True)
def robots_txt(request: AppRequest):
    lines = [
        "User-Agent: *",
        "Disallow: /admin/",
        "Disallow: /accounts/",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@require_GET
def favicon_ico(request: AppRequest):
    return HttpResponse(status=204)


@login_required
def stop_impersonation(request: AppRequest):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    impersonator_id = request.session.get(SESSION_IMPERSONATOR_USER_ID_KEY)
    if not impersonator_id:
        messages.warning(request, "You are not currently impersonating anyone.")
        return redirect("core:home")

    try:
        impersonator = User.objects.get(pk=impersonator_id, is_superuser=True)
    except User.DoesNotExist:
        messages.error(request, "Original user session could not be restored.")
        return redirect("core:home")

    backend = request.session.get(BACKEND_SESSION_KEY, settings.AUTHENTICATION_BACKENDS[0])
    login(request, impersonator, backend=backend)
    # Ensure the impersonator key is cleared after login (login flushes the session)
    request.session.pop(SESSION_IMPERSONATOR_USER_ID_KEY, None)

    posthog.capture(
        "user_impersonation_stopped",
        distinct_id=str(impersonator.pk),
    )
    messages.success(request, "Impersonation ended. You are now logged in as yourself.")
    return redirect("admin:index")


@login_required
def profile_edit(request: AppRequest):
    user = request.user

    if request.method == "POST":
        user_form = UserProfileForm(request.POST, instance=user, prefix="user")

        forms_valid = user_form.is_valid()

        if forms_valid:
            user_form.save()
            posthog.capture("profile_updated", properties={"fields": list(user_form.changed_data)})
            messages.success(request, "Profile updated successfully.")
            return redirect("core:profile")
    else:
        user_form = UserProfileForm(instance=user, prefix="user")

    return render(
        request,
        "core/profile.html",
        {
            "user_form": user_form,
        },
    )


def csrf_failure(request: AppRequest, reason=""):
    """Custom CSRF_FAILURE_VIEW: friendly page + PostHog capture.

    CsrfViewMiddleware calls this directly instead of raising an exception, so
    the underlying cause (stale token, broken connection mid-upload, missing
    CSRF_TRUSTED_ORIGINS entry, etc.) is otherwise invisible. Capturing it lets
    us see which `reason` actually dominates in production rather than
    guessing — see CLAUDE.md's Error Tracking conventions.
    """
    distinct_id = str(request.user.id) if request.user.is_authenticated else "csrf_failure"
    posthog.capture_exception(
        PermissionDenied(reason or "CSRF verification failed"),
        distinct_id=distinct_id,
        properties={
            "handler": "csrf_failure",
            "error_type": "CsrfFailure",
            "reason": reason,
            "path": request.path,
            "method": request.method,
        },
    )
    logger.warning("CSRF failure", reason=reason, path=request.path)
    return render(request, "core/csrf_failure.html", {"reason": reason}, status=403)
