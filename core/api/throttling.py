from django.conf import settings as django_settings
from ninja.throttling import AnonRateThrottle


class _SkipInTests(AnonRateThrottle):
    """Base throttle that no-ops under pytest.

    Throttle counters live in the shared cache, so without this a test suite that
    logs in more than the limit would start failing on request count rather than
    on behavior.
    """

    _TESTING: bool = getattr(django_settings, "TESTING", False)  # evaluated once at class creation

    def allow_request(self, request) -> bool:
        if self._TESTING:
            return True
        return super().allow_request(request)


class LoginRateThrottle(_SkipInTests):
    """10 attempts per 5 minutes per IP — slows credential-stuffing on /auth/token."""

    scope = "login"


class RefreshRateThrottle(_SkipInTests):
    """30 attempts per 5 minutes per IP — prevents token replay floods on /auth/refresh."""

    scope = "refresh"


class RegisterRateThrottle(_SkipInTests):
    """5 attempts per 15 minutes per IP — limits account-creation abuse on /auth/register."""

    scope = "register"


class VerifyRateThrottle(_SkipInTests):
    """10 attempts per 5 minutes per IP — limits brute-force on /auth/verify-email."""

    scope = "verify"


class ResendRateThrottle(_SkipInTests):
    """3 attempts per 15 minutes per IP — limits resend spam on /auth/resend-verification."""

    scope = "resend"
