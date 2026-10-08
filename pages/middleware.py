"""Site password gate.

The session helpers are the contract every other slice uses: a save-the-date page calls
`unlock_site()` so a guest holding that link never sees the password page. The session
stores the *fingerprint* of the current password rather than a flag, so rotating the
password in the admin re-locks every session at once.
"""

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django_htmx.http import HttpResponseClientRedirect

from pages.models import WeddingSettings

SESSION_UNLOCK_KEY = "site_unlocked"


def unlock_site(request) -> None:
    """Mark this session as past the gate for the current password (rotating re-locks)."""
    request.session[SESSION_UNLOCK_KEY] = WeddingSettings.load().site_password_fingerprint


def site_is_unlocked(request) -> bool:
    wedding = WeddingSettings.load()
    if not wedding.gate_enabled:
        return True
    return request.session.get(SESSION_UNLOCK_KEY) == wedding.site_password_fingerprint


class SitePasswordMiddleware:
    """Redirect locked visitors to the password page while `WeddingSettings.site_password` is set.

    Sits right after `AuthenticationMiddleware` because staff bypass needs `request.user`.
    The decision is made in `process_view`, where `request.resolver_match` is populated, so
    exemptions are declared by URL namespace and name rather than by path prefix. The two
    exceptions are `STATIC_URL` and `MEDIA_URL`: whitenoise normally answers those before we
    run, but the DEBUG media route has no URL name to match on.
    """

    # Nested includes: the admin, the Resend webhook, every save-the-date page (holding
    # that link is the credential), and the two DEBUG-only tool apps.
    EXEMPT_NAMESPACES = frozenset({"admin", "anymail", "save_the_date", "djdt", "django_browser_reload"})
    # Top-level routes (`namespace:name` for namespaced ones).
    EXEMPT_VIEW_NAMES = frozenset({"pages:welcome", "health", "robots_txt", "favicon_ico"})
    # allauth registers its account and MFA routes without a namespace; the url names
    # carry the prefix instead (`account_login`, `mfa_index`, ...).
    EXEMPT_URL_NAME_PREFIXES = ("account_", "mfa_")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs) -> HttpResponse | None:
        # Route exemptions come first, before anything touches the database: /health/
        # has to answer even when the database is down.
        if self._route_is_exempt(request):
            return None
        if not WeddingSettings.load().gate_enabled:
            return None
        if self._is_staff(request) or site_is_unlocked(request):
            return None

        welcome_url = f"{reverse('pages:welcome')}?{urlencode({'next': request.get_full_path()})}"
        if getattr(request, "htmx", False):
            return HttpResponseClientRedirect(welcome_url)
        return redirect(welcome_url)

    @staticmethod
    def _is_staff(request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        return user is not None and user.is_authenticated and (user.is_staff or user.is_superuser)

    def _route_is_exempt(self, request: HttpRequest) -> bool:
        if request.path.startswith((settings.STATIC_URL, settings.MEDIA_URL)):
            return True

        match = request.resolver_match
        if match is None:
            return False
        if self.EXEMPT_NAMESPACES.intersection(match.namespaces):
            return True
        if match.view_name in self.EXEMPT_VIEW_NAMES:
            return True
        return bool(match.url_name) and match.url_name.startswith(self.EXEMPT_URL_NAME_PREFIXES)
