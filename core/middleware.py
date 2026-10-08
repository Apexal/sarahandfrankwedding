from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse


class TrustedClientIPMiddleware:
    """Rewrite REMOTE_ADDR from a trusted proxy header (e.g. Fly-Client-IP).

    Behind Fly's proxy, REMOTE_ADDR is the proxy's address, which breaks
    everything keyed on client IP: allauth's login rate limits collapse into
    a single shared bucket, and structlog's ip records the proxy instead of
    the client. When TRUSTED_CLIENT_IP_HEADER is set, this middleware replaces
    REMOTE_ADDR with that header's value so every downstream consumer sees
    the real client IP.

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


class RequireStaffMfaMiddleware:
    """Force staff and superuser accounts to enroll in MFA before using the app.

    allauth already challenges for the second factor at login once an
    authenticator exists; this closes the enrollment gap by redirecting
    privileged accounts that have no TOTP/WebAuthn authenticator to the MFA
    setup page. Recovery codes alone don't count — they only exist alongside
    a real second factor.

    Controlled by MFA_REQUIRED_FOR_STAFF (on by default; .env.example turns
    it off for local development).
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
