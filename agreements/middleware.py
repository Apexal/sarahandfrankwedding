from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django_htmx.http import HttpResponseClientRedirect

from agreements.services import has_pending_agreements
from core.constants import SESSION_IMPERSONATOR_USER_ID_KEY

# Paths that must stay reachable while a user is gated. Allauth flows are exempt so
# a blocked user can still log out, reset a password, or confirm an email; the admin
# is listed for completeness even though staff are exempt outright.
EXEMPT_PREFIXES = ("/accounts/", "/admin/", "/health/")


class AgreementGateMiddleware:
    """Send users who owe a required agreement to the acceptance page.

    Runs as `process_view` so `request.resolver_match` is populated: the agreements
    app is identified by its URL namespace rather than a hardcoded path, which keeps
    the acceptance page reachable no matter where the URLconf mounts it. Without that
    the gate would redirect the very page it redirects to.

    Staff and superusers are exempt — a bad agreement must never lock the people who
    can fix it out of the admin (the same carve-out MaintenanceModeMiddleware makes).
    Impersonation is exempt too: the real actor is a superuser, and accepting legal
    terms on someone else's behalf is not something we want recorded.

    Must be placed after AuthenticationMiddleware (needs `request.user`), SessionMiddleware
    (reads the impersonation key), and HtmxMiddleware (needs `request.htmx`).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not self._is_gated(request):
            return None
        if not has_pending_agreements(request.user):
            return None

        # Deliberately no messages.info here: one navigation can resolve several
        # requests (an open HTMX poll, /favicon.ico, a preloaded link), and each would
        # queue its own copy to be rendered as a stack of identical alerts. The pending
        # page states why the user is there.
        target = reverse("agreements:pending")
        if getattr(request, "htmx", False):
            # A plain redirect would be swapped into the target element instead of navigating.
            return HttpResponseClientRedirect(target)
        return redirect(target)

    def _is_gated(self, request) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return False
        if request.session.get(SESSION_IMPERSONATOR_USER_ID_KEY):
            return False
        if request.resolver_match and request.resolver_match.app_name == "agreements":
            return False
        return not request.path.startswith((*EXEMPT_PREFIXES, settings.STATIC_URL))
