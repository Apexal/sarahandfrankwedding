from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from auditlog.context import set_extra_data
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from core.constants import SESSION_IMPERSONATOR_USER_ID_KEY
from core.models import AppSettings, User
from core.utils.timezone_utils import get_user_tz


class TrustedClientIPMiddleware:
    """Rewrite REMOTE_ADDR from a trusted proxy header (e.g. Fly-Client-IP).

    Behind Fly's proxy, REMOTE_ADDR is the proxy's address, which breaks
    everything keyed on client IP: allauth's login rate limits collapse into
    a single shared bucket, and auditlog's remote_addr / structlog's ip
    record the proxy instead of the client. When TRUSTED_CLIENT_IP_HEADER
    is set, this middleware replaces REMOTE_ADDR with that header's value so
    every downstream consumer sees the real client IP.

    Only set TRUSTED_CLIENT_IP_HEADER when all traffic arrives through a
    proxy that overwrites the header (Fly's does); otherwise clients could
    spoof their IP. Must be first in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        header = getattr(settings, "TRUSTED_CLIENT_IP_HEADER", None)
        if header:
            client_ip = request.headers.get(header)
            if client_ip:
                request.META["REMOTE_ADDR"] = client_ip.strip()
        return self.get_response(request)


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if AppSettings.load().maintenance_mode:
            if request.path == "/health/":
                pass
            elif not (request.user.is_authenticated and request.user.is_staff):
                html = render_to_string("503.html", request=request)
                return HttpResponse(html, status=503)
        return self.get_response(request)


class ImpersonationAuditMiddleware:
    """Override auditlog actor during impersonation sessions.

    When a superuser is impersonating another user, this middleware ensures
    that audit log entries attribute changes to the impersonator (the real
    actor) and record the impersonated user in additional_data.

    Must be placed after AuditlogMiddleware in MIDDLEWARE so that it wraps
    the response processing inside auditlog's context.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        impersonator_id = getattr(request, "session", {}).get(SESSION_IMPERSONATOR_USER_ID_KEY)
        if not impersonator_id:
            return self.get_response(request)

        try:
            impersonator = User.objects.get(pk=impersonator_id)
        except User.DoesNotExist:
            return self.get_response(request)

        context_data = {
            "actor": impersonator,
            "additional_data": {
                "impersonation": True,
                "impersonated_user_id": request.user.pk,
                "impersonated_username": request.user.username,
            },
        }
        with set_extra_data(context_data):
            return self.get_response(request)


class RequireStaffMfaMiddleware:
    """Force staff and superuser accounts to enroll in MFA before using the app.

    allauth already challenges for the second factor at login once an
    authenticator exists; this closes the enrollment gap by redirecting
    privileged accounts that have no TOTP/WebAuthn authenticator to the MFA
    setup page. Recovery codes alone don't count — they only exist alongside
    a real second factor.

    Controlled by MFA_REQUIRED_FOR_STAFF (on by default; .env.example turns
    it off for local development). Skipped during impersonation: the real
    actor is a superuser who already passed their own MFA challenge.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._exempt_prefixes = None

    def _get_exempt_prefixes(self):
        # Resolved lazily so the URLconf is fully loaded, then cached.
        if self._exempt_prefixes is None:
            self._exempt_prefixes = (
                reverse("mfa_index"),  # /accounts/2fa/ — covers all MFA setup routes
                reverse("account_logout"),
                reverse("account_reauthenticate"),
                settings.STATIC_URL,
                "/health/",
            )
        return self._exempt_prefixes

    def _needs_mfa_setup(self, request):
        user = request.user
        if not user.is_authenticated or not (user.is_staff or user.is_superuser):
            return False
        if request.session.get(SESSION_IMPERSONATOR_USER_ID_KEY):
            return False
        if request.path.startswith(self._get_exempt_prefixes()):
            return False
        return not is_mfa_enabled(user, types=[Authenticator.Type.TOTP, Authenticator.Type.WEBAUTHN])

    def __call__(self, request):
        if settings.MFA_REQUIRED_FOR_STAFF and self._needs_mfa_setup(request):
            messages.warning(
                request,
                "Two-factor authentication is required for staff accounts. "
                "Set up an authenticator app or add a passkey to continue.",
            )
            return redirect("mfa_index")
        return self.get_response(request)


class UserTimezoneMiddleware:
    """Activate the logged-in user's timezone for each request.

    Falls back to `settings.TIME_ZONE` when the user has no timezone set or is anonymous.
    This ensures Django's template filters and admin widgets use the correct timezone.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        tz = get_user_tz(user) if user and getattr(user, "is_authenticated", False) else get_user_tz(None)
        timezone.activate(tz)
        response = self.get_response(request)
        timezone.deactivate()
        return response
