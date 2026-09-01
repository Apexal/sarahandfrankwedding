"""JSON REST API for the mobile app (django-ninja).

Kept separate from the HTMX views: those use session auth via allauth, this uses
opaque bearer tokens, because a mobile client has no cookie jar to hang a session
on. Tokens are modelled by `core.models.AccessToken` (access_token) and
`core.models.RefreshToken` (refresh_token); see `core.api.auth` for the bearer
scheme.

The two halves share one user table, so a person can sign in on the web and in
the app with the same credentials. Registration and email verification are
duplicated rather than shared: allauth emails a confirmation *link*, which a
mobile app can't complete, so the API issues a 6-digit code instead
(`core.models.EmailVerificationCode`) and marks the allauth `EmailAddress`
verified when the code is redeemed.

Add domain routers with `api.add_router("/things/", things_router)`.
"""

import logging
import re
from datetime import timedelta
from uuid import uuid4

import posthog
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.errors import AuthenticationError, HttpError

from core.api.account import router as account_router
from core.api.auth import TokenAuth
from core.api.emails import send_templated_email
from core.api.throttling import (
    LoginRateThrottle,
    RefreshRateThrottle,
    RegisterRateThrottle,
    ResendRateThrottle,
    VerifyRateThrottle,
)
from core.audit import record_auth_event
from core.models import AccessToken, AppSettings, EmailVerificationCode, RefreshToken, User

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="starter_app API",
    version="1.0.0",
    auth=TokenAuth(),
    # The schema at /api/openapi.json is what the mobile client generates from, so it
    # stays open in development and behind an admin login everywhere else.
    docs_decorator=None if settings.ENV == "development" else staff_member_required,
)

api.add_router("/account/", account_router)

# Access tokens are short-lived; the app trades the long-lived refresh token for a
# fresh one when it sees a 401. See `/auth/refresh`.
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=90)

_MAX_PASSWORD_LENGTH = 128  # guard against hashing-DoS on huge inputs
_MIN_PHONE_DIGITS = 7
_MAX_PHONE_DIGITS = 15


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(Schema):
    email: str
    password: str
    device_name: str = ""


class VerifyEmailRequest(Schema):
    email: str
    code: str
    device_name: str = ""


class ResendVerificationRequest(Schema):
    email: str


class DetailResponse(Schema):
    detail: str


class TokenRequest(Schema):
    email: str
    password: str
    device_name: str = ""


class RefreshRequest(Schema):
    refresh_token: str


class LogoutRequest(Schema):
    refresh_token: str


class TokenPair(Schema):
    access_token: str
    refresh_token: str


class MeResponse(Schema):
    id: int
    email: str
    first_name: str
    last_name: str
    phone_number: str
    time_zone: str


class UpdateMeRequest(Schema):
    first_name: str
    last_name: str


class UpdatePhoneRequest(Schema):
    phone_number: str


class ChangePasswordRequest(Schema):
    current_password: str
    new_password: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_pair(user, *, name: str = "", family=None) -> dict:
    # Access + refresh from one login share a family, so a breach revokes both. New login → new family.
    family = family or uuid4()
    _, access_token = AccessToken.issue(user, name=name, ttl=ACCESS_TOKEN_TTL, family=family)
    _, refresh_token = RefreshToken.issue(user, name=name, ttl=REFRESH_TOKEN_TTL, family=family)
    return {"access_token": access_token, "refresh_token": refresh_token}


def _me_response(user: User) -> dict:
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone_number": user.phone_number,
        "time_zone": user.time_zone,
    }


def _log_failed_login(request, *, email: str, device_name: str) -> None:
    """Log a failed login attempt, distinguishing inactive accounts from bad credentials.

    The auth backends return None for both wrong passwords and inactive accounts
    (via user_can_authenticate). A secondary lookup lets us tell them apart in the
    audit trail without leaking the difference in the response.
    """
    try:
        candidate = User.objects.get(email__iexact=email)
        reason = "account_inactive" if not candidate.is_active else "invalid_credentials"
        record_auth_event(
            event="api_login_failed",
            user=candidate if not candidate.is_active else None,
            request=request,
            additional_data={"reason": reason, "device_name": device_name},
        )
    except User.DoesNotExist:
        record_auth_event(
            event="api_login_failed",
            request=request,
            additional_data={"reason": "invalid_credentials", "device_name": device_name},
        )


def _send_verification_code(user: User) -> None:
    """Issue a fresh `EmailVerificationCode` for *user* and dispatch the email."""
    _, raw_code = EmailVerificationCode.issue(user)
    send_templated_email(
        template_key="verification_code",
        to_email=user.email,
        subject="Verify your email address",
        context={
            "otp_code": raw_code,
            "expires_minutes": int(EmailVerificationCode.CODE_TTL.total_seconds() // 60),
            "site_name": AppSettings.load().site_name,
        },
    )


def _validate_password(raw_password: str, user: User) -> None:
    """Apply AUTH_PASSWORD_VALIDATORS, raising a 422 with the collected messages."""
    if len(raw_password) > _MAX_PASSWORD_LENGTH:
        raise HttpError(422, f"Password must be {_MAX_PASSWORD_LENGTH} characters or fewer.")
    try:
        password_validation.validate_password(raw_password, user)
    except DjangoValidationError as exc:
        raise HttpError(422, " ".join(exc.messages)) from exc


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

_REGISTER_NEUTRAL_RESPONSE = {
    "detail": "We've sent a verification code to that email address. Enter it to complete registration."
}
_VERIFY_NEUTRAL_ERROR = "Invalid or expired code."


@api.post("/auth/register", tags=["auth"], auth=None, throttle=RegisterRateThrottle("5/15min"), response=DetailResponse)
def register(request, payload: RegisterRequest):
    """Create a new inactive account and dispatch an email verification code.

    Returns the same neutral response whether the email is new, already-unverified,
    or already-registered — this prevents email enumeration. For an existing
    unverified account a fresh code is re-issued. Tokens are only issued once the
    address is verified via `POST /auth/verify-email`.
    """
    if not AppSettings.load().allow_registration:
        raise HttpError(403, "Registration is closed.")

    email = User.objects.normalize_email(payload.email)

    # Validate strength before hashing. An unsaved User lets
    # UserAttributeSimilarityValidator compare against the email address.
    _validate_password(payload.password, User(email=email))

    # Handle duplicate emails without leaking existence.
    existing = User.objects.filter(email__iexact=email).first()
    if existing is not None:
        record_auth_event(
            event="api_register_failed",
            user=existing,
            request=request,
            additional_data={"reason": "email_exists", "device_name": payload.device_name},
        )
        if not existing.is_active:
            # Unverified account: re-issue a code so they can complete registration.
            _send_verification_code(existing)
            record_auth_event(event="api_email_verify_sent", user=existing, request=request)
        # Verified account: no action — never reveal existence, never send email.
        return _REGISTER_NEUTRAL_RESPONSE

    # New account, created inactive. IntegrityError catches a concurrent duplicate
    # that slipped past the check above (TOCTOU race).
    try:
        with transaction.atomic():
            user = User(email=email, is_active=False)
            # `username` is still USERNAME_FIELD for the admin; allauth's adapter
            # derives a unique one so API signups match web signups.
            get_adapter().populate_username(request, user)
            user.set_password(payload.password)
            user.save()
            EmailAddress.objects.create(user=user, email=user.email, verified=False, primary=True)
    except IntegrityError:
        logger.warning("Registration race for email %s", email, exc_info=True)
        return _REGISTER_NEUTRAL_RESPONSE

    _send_verification_code(user)
    record_auth_event(
        event="api_register",
        user=user,
        request=request,
        additional_data={"device_name": payload.device_name},
    )
    record_auth_event(event="api_email_verify_sent", user=user, request=request)
    posthog.capture("api_user_registered", distinct_id=str(user.pk), properties={"device_name": payload.device_name})
    return _REGISTER_NEUTRAL_RESPONSE


@api.post("/auth/verify-email", tags=["auth"], auth=None, throttle=VerifyRateThrottle("10/5min"), response=TokenPair)
def verify_email(request, payload: VerifyEmailRequest):
    """Verify an email address with the 6-digit code sent at registration.

    On success the account is activated, its allauth `EmailAddress` is marked
    verified (so the same person can also sign in on the web), and a token pair
    is returned. A single neutral 422 covers all failure modes to prevent
    enumeration.
    """
    email = User.objects.normalize_email(payload.email)

    # Look up an inactive (unverified) user only — verified users cannot re-verify.
    try:
        user = User.objects.get(email__iexact=email, is_active=False)
    except User.DoesNotExist:
        record_auth_event(
            event="api_email_verify_failed",
            request=request,
            additional_data={"reason": "user_not_found_or_already_active"},
        )
        raise HttpError(422, _VERIFY_NEUTRAL_ERROR) from None

    code_row = user.email_verification_codes.filter(consumed_at__isnull=True).order_by("-created_at").first()

    if code_row is None:
        record_auth_event(
            event="api_email_verify_failed",
            user=user,
            request=request,
            additional_data={"reason": "no_pending_code"},
        )
        raise HttpError(422, _VERIFY_NEUTRAL_ERROR)

    if not code_row.is_redeemable:
        reason = "max_attempts_reached" if code_row.attempts >= EmailVerificationCode.MAX_ATTEMPTS else "expired"
        record_auth_event(
            event="api_email_verify_failed",
            user=user,
            request=request,
            additional_data={"reason": reason},
        )
        raise HttpError(422, _VERIFY_NEUTRAL_ERROR)

    if not code_row.verify(payload.code):
        record_auth_event(
            event="api_email_verify_failed",
            user=user,
            request=request,
            additional_data={"reason": "invalid_code"},
        )
        raise HttpError(422, _VERIFY_NEUTRAL_ERROR)

    # Code accepted — activate the account on both halves of the app.
    user.is_active = True
    user.save(update_fields=["is_active", "updated_at"])
    EmailAddress.objects.update_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})

    record_auth_event(
        event="api_email_verified",
        user=user,
        request=request,
        additional_data={"device_name": payload.device_name},
    )
    posthog.capture("api_email_verified", distinct_id=str(user.pk), properties={"device_name": payload.device_name})
    return _issue_pair(user, name=payload.device_name)


@api.post(
    "/auth/resend-verification",
    tags=["auth"],
    auth=None,
    throttle=ResendRateThrottle("3/15min"),
    response=DetailResponse,
)
def resend_verification(request, payload: ResendVerificationRequest):
    """Re-send an email verification code to an unverified account.

    Always returns the same neutral response regardless of whether the account
    exists or is already verified — prevents email enumeration.
    """
    email = User.objects.normalize_email(payload.email)
    neutral = {"detail": "If an account for that address is awaiting verification, a new code has been sent."}

    try:
        user = User.objects.get(email__iexact=email, is_active=False)
    except User.DoesNotExist:
        return neutral  # no-op: user not found or already verified

    _send_verification_code(user)
    record_auth_event(event="api_email_verify_sent", user=user, request=request)
    return neutral


@api.post("/auth/token", tags=["auth"], auth=None, throttle=LoginRateThrottle("10/5min"), response=TokenPair)
def obtain_token(request, payload: TokenRequest):
    """Exchange email + password for an access/refresh pair. Credentials go in the body, never the URL."""
    user = authenticate(request, email=payload.email, password=payload.password)
    if user is None:
        # The backends return None for both wrong passwords AND inactive accounts.
        # Do a targeted lookup so we can log the real reason without leaking it.
        _log_failed_login(request, email=payload.email, device_name=payload.device_name)
        raise AuthenticationError
    record_auth_event(
        event="api_login", user=user, request=request, additional_data={"device_name": payload.device_name}
    )
    posthog.capture("api_token_issued", distinct_id=str(user.pk), properties={"device_name": payload.device_name})
    user.last_login = timezone.now()
    user.save(update_fields=["last_login", "updated_at"])
    return _issue_pair(user, name=payload.device_name)


@api.post("/auth/refresh", tags=["auth"], auth=None, throttle=RefreshRateThrottle("30/5min"), response=TokenPair)
def refresh_token(request, payload: RefreshRequest):
    """Trade a valid refresh token for a new pair.

    The presented token is rotated (single-use). Reuse of an already-rotated token is
    treated as theft: the entire token family is revoked, forcing a fresh login.
    """
    token = RefreshToken.find(payload.refresh_token)
    if token is None:
        raise AuthenticationError  # unknown token
    if token.revoked_at is not None:
        # Breach: a token that was already rotated (or revoked) is being replayed.
        # Kill the whole family — both refresh tokens AND the access tokens minted
        # alongside them — so the attacker AND the legitimate holder are logged out
        # immediately, not merely blocked from refreshing.
        revoked = RefreshToken.revoke_family(token.family) + AccessToken.revoke_family(token.family)
        record_auth_event(
            event="api_token_breach_detected",
            user=token.user,
            request=request,
            additional_data={"family": str(token.family), "tokens_revoked": revoked},
        )
        posthog.capture(
            "api_refresh_token_reuse_detected",
            distinct_id=str(token.user_id),
            properties={"family": str(token.family), "tokens_revoked": revoked},
        )
        raise AuthenticationError
    if not token.is_active:
        raise AuthenticationError  # merely expired (or inactive user) → re-login, not a breach
    # Atomic rotation: only the first concurrent request to present this token wins.
    # filter().update() is a single atomic SQL UPDATE, so at most one caller sees rotated=1.
    with transaction.atomic():
        rotated = RefreshToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), updated_at=timezone.now()
        )
    if not rotated:
        raise AuthenticationError  # lost the race — another request already rotated this token
    record_auth_event(
        event="api_token_refresh", user=token.user, request=request, additional_data={"device_name": token.name}
    )
    posthog.capture("api_token_refreshed", distinct_id=str(token.user_id))
    return _issue_pair(token.user, name=token.name, family=token.family)


@api.post("/auth/logout", tags=["auth"], response=DetailResponse)
def logout(request, payload: LogoutRequest):
    """Revoke the current access token and the supplied refresh token."""
    access: AccessToken = request.auth
    access.revoke()
    refresh_token = RefreshToken.resolve(payload.refresh_token)
    if refresh_token is not None:
        refresh_token.revoke()
    record_auth_event(event="api_logout", user=access.user, request=request)
    posthog.capture("api_token_revoked", distinct_id=str(access.user_id), properties={"reason": "logout"})
    return {"detail": "Logged out."}


# ---------------------------------------------------------------------------
# Current-user endpoints
# ---------------------------------------------------------------------------


@api.get("/me", tags=["user"], response=MeResponse)
def me(request):
    return _me_response(request.auth.user)


@api.patch("/me", tags=["user"], response=MeResponse)
def update_me(request, payload: UpdateMeRequest):
    """Update the authenticated user's name."""
    user: User = request.auth.user
    user.first_name = payload.first_name.strip()
    user.last_name = payload.last_name.strip()
    user.save(update_fields=["first_name", "last_name", "updated_at"])
    return _me_response(user)


@api.patch("/me/phone", tags=["user"], response=MeResponse)
def update_phone(request, payload: UpdatePhoneRequest):
    """Update the authenticated user's phone number.

    Accepts any common format; `User.save()` stores normalized E.164. Send an
    empty string to clear it.
    """
    user: User = request.auth.user
    raw = payload.phone_number.strip()

    if raw:
        digit_count = len(re.sub(r"\D", "", raw))
        if digit_count < _MIN_PHONE_DIGITS or digit_count > _MAX_PHONE_DIGITS:
            raise HttpError(422, "Please enter a valid phone number.")

    user.phone_number = raw
    user.save(update_fields=["phone_number", "updated_at"])
    return _me_response(user)


@api.post("/me/password", tags=["user"], response=DetailResponse)
def change_password(request, payload: ChangePasswordRequest):
    """Change the authenticated user's password.

    Verifies the current password before accepting the new one. On success, all
    other tokens for the account are revoked so other devices are signed out.
    """
    user: User = request.auth.user

    if not user.check_password(payload.current_password):
        raise HttpError(422, "Current password is incorrect.")

    _validate_password(payload.new_password, user)

    user.set_password(payload.new_password)
    user.save(update_fields=["password", "updated_at"])

    now = timezone.now()
    RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=now, updated_at=now)
    AccessToken.objects.filter(user=user, revoked_at__isnull=True).exclude(pk=request.auth.pk).update(
        revoked_at=now, updated_at=now
    )

    return {"detail": "Password updated."}
