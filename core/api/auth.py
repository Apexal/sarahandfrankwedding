import contextlib

from auditlog.context import auditlog_value
from ninja.security import HttpBearer

from core.models import AccessToken, User


class TokenAuth(HttpBearer):
    """Validate the `Authorization: Bearer <token>` header against an AccessToken row.

    On success `request.auth` is the AccessToken (so handlers can revoke it) and
    `request.user` is set to the owning user for downstream code (PostHog, auditlog).

    Inactive users are rejected. If your app's API should only serve a subset of
    users (patients but not clinicians, members but not staff, …), add that check
    here — it is the single choke point every authenticated endpoint passes through.
    """

    def authenticate(self, request, token):
        access_token = AccessToken.resolve(token)
        if access_token is None:
            return None  # django-ninja turns this into a 401
        access_token.touch()
        request.user = User.objects.get(pk=access_token.user_id)
        if not request.user.is_active:
            return None
        # AuditlogMiddleware sets the actor from request.user before Ninja's auth runs,
        # so it captures AnonymousUser for bearer-token requests. Update it here so any
        # model saves made during this request are attributed to the correct user.
        with contextlib.suppress(LookupError):
            auditlog_value.get()["actor"] = access_token.user
        return access_token
