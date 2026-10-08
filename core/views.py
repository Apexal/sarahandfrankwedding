import structlog
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

from core.types import AppRequest

logger = structlog.get_logger(__name__)


def health(request: AppRequest):
    """Health check with database connectivity verification."""
    try:
        connection.ensure_connection()
        return JsonResponse({"status": "ok", "database": "connected"})
    except Exception as exc:
        # Don't echo exception details to unauthenticated callers — they go to the logs instead.
        logger.exception("health_check_failed", handler="health", error_type=type(exc).__name__)
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)


@require_GET
@cache_control(max_age=86400, public=True)
def robots_txt(request: AppRequest):
    # A private site: nothing is meant to be indexed. The noindex meta tag in
    # core/base.html covers pages a crawler reaches through a shared link.
    lines = ["User-Agent: *", "Disallow: /"]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@require_GET
def favicon_ico(request: AppRequest):
    return HttpResponse(status=204)


def csrf_failure(request: AppRequest, reason=""):
    """Custom CSRF_FAILURE_VIEW: friendly page + a logged reason.

    CsrfViewMiddleware calls this directly instead of raising an exception, so
    the underlying cause (stale token, broken connection mid-upload, missing
    CSRF_TRUSTED_ORIGINS entry, etc.) is otherwise invisible. Logging it lets
    us see which `reason` actually dominates in production rather than
    guessing — see CLAUDE.md's Error Tracking conventions.
    """
    logger.warning("csrf_failure", reason=reason, path=request.path, method=request.method)
    return render(request, "core/csrf_failure.html", {"reason": reason}, status=403)
