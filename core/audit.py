from auditlog import get_logentry_model
from auditlog.middleware import AuditlogMiddleware
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType


def record_object_access(*, accessed_object, actor, remote_addr=None, additional_data=None):
    """Write an ACCESS audit entry recording that `actor` viewed `accessed_object`.

    Call this when a user opens a specific sensitive record — e.g. a clinician
    opening a client's audio recording. Do NOT call it for list views or for a
    user reading their own data; reserve it for "who looked at whose record".
    """
    log_entry_model = get_logentry_model()
    content_type = ContentType.objects.get_for_model(accessed_object.__class__)
    return log_entry_model.objects.create(
        content_type=content_type,
        object_pk=str(accessed_object.pk),
        object_id=accessed_object.pk if isinstance(accessed_object.pk, int) else None,
        object_repr=str(accessed_object),
        action=log_entry_model.Action.ACCESS,
        actor=actor,
        remote_addr=remote_addr,
        additional_data=additional_data or None,
    )


def record_auth_event(
    *, event: str, user=None, request=None, remote_addr: str | None = None, additional_data: dict | None = None
):
    """Write an auth event (login, logout, refresh, breach) to the audit log.

    Session auth on the web side is already covered by allauth's signals in
    `core/signals.py`; this is the equivalent trail for the token-authenticated
    mobile API, where there is no session to hook.

    `user` is the relevant User for the event; pass None for failures where no
    user was identified (e.g. a login attempt with an unknown email).
    `remote_addr` is extracted from `request` when not supplied directly.

    Event strings used by `core/api`:
        api_login                 — successful login
        api_login_failed          — rejected login (see additional_data["reason"])
        api_token_refresh         — successful token rotation
        api_token_breach_detected — revoked-token replay; whole family killed
        api_logout                — explicit logout
        api_register              — successful self-registration
        api_register_failed       — rejected registration (see additional_data["reason"])
        api_email_verify_sent     — OTP code issued and dispatched (register or resend)
        api_email_verified        — correct OTP submitted; account activated
        api_email_verify_failed   — OTP check failed (see additional_data["reason"])
    """
    log_entry_model = get_logentry_model()
    content_type = ContentType.objects.get_for_model(get_user_model())

    if remote_addr is None and request is not None:
        remote_addr = AuditlogMiddleware._get_remote_addr(request)

    return log_entry_model.objects.create(
        content_type=content_type,
        object_pk=str(user.pk) if user else "",
        object_id=user.pk if user else None,
        object_repr=str(user) if user else "(unknown user)",
        action=log_entry_model.Action.ACCESS,
        actor=user,
        remote_addr=remote_addr,
        additional_data={"event": event, **(additional_data or {})},
    )
