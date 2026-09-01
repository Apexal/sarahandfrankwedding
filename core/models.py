import hashlib
import re
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from auditlog.models import AuditlogHistoryField
from auditlog.registry import auditlog
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.core.cache import cache
from django.db import models
from django.db.models import F
from django.utils import timezone

from core.constants import get_timezone_choices


class TimestampedModel(models.Model):
    """
    All models that need created_at and updated_at fields can inherit from this.
    Generally all models should use it.
    """

    created_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(TimestampedModel, AbstractUser):
    history = AuditlogHistoryField()
    email = models.EmailField(blank=False, unique=True)
    phone_number = models.CharField(
        max_length=16,
        blank=True,
    )
    time_zone = models.CharField(max_length=50, blank=True, choices=get_timezone_choices)

    REQUIRED_FIELDS = ["email"]

    def __str__(self) -> str:
        return self.name or self.email

    def save(self, *args, **kwargs):
        if self.phone_number:
            self.phone_number = self._normalize_phone_number(self.phone_number)
        return super().save(*args, **kwargs)

    @property
    def name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        else:
            return None

    def _normalize_phone_number(self, phone_number: str) -> str:
        raw = phone_number.strip()
        has_plus = raw.startswith("+")
        digits = re.sub(r"\D", "", raw)
        if raw.startswith("00"):
            digits = digits[2:]
        if not has_plus and len(digits) == 10:
            digits = "1" + digits  # assume US if no country code
        digits = digits[:15]  # E.164 allows up to 15 digits
        return f"+{digits}" if digits else ""


class AppSettings(TimestampedModel):
    CACHE_KEY = "app_settings"
    history = AuditlogHistoryField()

    site_name = models.CharField(max_length=100, default="My App")
    support_email = models.EmailField(default="support@example.com")
    maintenance_mode = models.BooleanField(default=False, help_text="Show a maintenance page to non-staff users.")
    allow_registration = models.BooleanField(
        default=False,
        help_text=(
            "Let new people create accounts, on the website and in the app alike. "
            "Off closes both signup paths; everyone who already has an account signs in as usual."
        ),
    )

    class Meta:
        verbose_name = "App Settings"
        verbose_name_plural = "App Settings"

    def __str__(self):
        return "App Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def load(cls):
        settings = cache.get(cls.CACHE_KEY)
        if settings is None:
            settings, _ = cls.objects.get_or_create(pk=1)
            cache.set(cls.CACHE_KEY, settings)
        return settings


class HashedToken(TimestampedModel):
    """
    Abstract base for the opaque bearer/refresh tokens used by the mobile API.

    The raw token is never stored — only its SHA-256 hash. Because the raw value
    is high-entropy (`secrets.token_urlsafe(32)`, ~256 bits) a plain unsalted hash
    is appropriate and lets us look the token up with an indexed query. The raw
    token is returned exactly once, at issue time. Concrete subclasses add the
    `user` FK (with their own `related_name`).
    """

    name = models.CharField(max_length=255, blank=True, help_text="Device label, e.g. 'Alex's iPhone'.")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    # Tokens minted together for one device login share a `family`, so a breach can revoke the whole set.
    family = models.UUIDField(default=uuid4, editable=False, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name or 'token'} ({self.user})"

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, *, name: str = "", ttl=None, **extra_fields):
        """Create a token for `user`. Returns (row, raw_token); show the raw token once."""
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name,
            token_hash=cls._hash(raw),
            expires_at=timezone.now() + ttl if ttl else None,
            **extra_fields,
        )
        return token, raw

    @classmethod
    def resolve(cls, raw: str):
        """Return the active token matching `raw`, or None if missing/expired/revoked/inactive user."""
        try:
            token = cls.objects.select_related("user").get(token_hash=cls._hash(raw))
        except cls.DoesNotExist:
            return None
        if not token.is_active:
            return None
        return token

    @property
    def is_active(self) -> bool:
        if self.revoked_at or not self.user.is_active:
            return False
        return not (self.expires_at and self.expires_at < timezone.now())

    def touch(self) -> None:
        """Record usage, written per request."""
        type(self).objects.filter(pk=self.pk).update(last_used_at=timezone.now())

    def revoke(self) -> None:
        if not self.revoked_at:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])

    @classmethod
    def revoke_family(cls, family: UUID) -> int:
        """Revoke every still-active token of this type in `family`. Returns the count revoked."""
        return cls.objects.filter(family=family, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), updated_at=timezone.now()
        )


class AccessToken(HashedToken):
    """Short-lived access token sent on every API request as `Authorization: Bearer`."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="access_tokens")

    if TYPE_CHECKING:
        user_id: int


class RefreshToken(HashedToken):
    """Long-lived token, sent only to `/api/auth/refresh` to mint a new access token.

    Single-use: `/auth/refresh` revokes the presented token and issues a new one
    (rotation). Every token in a rotation chain shares a `family` id; reuse of an
    already-rotated token is a theft signal that revokes the whole family.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="refresh_tokens")

    @classmethod
    def find(cls, raw: str):
        """Look up by raw token regardless of state (may be expired/revoked). For breach detection."""
        try:
            return cls.objects.select_related("user").get(token_hash=cls._hash(raw))
        except cls.DoesNotExist:
            return None

    if TYPE_CHECKING:
        user_id: int


class EmailVerificationCode(TimestampedModel):
    """Single-use 6-digit OTP for email verification at API registration.

    The web signup flow uses allauth's emailed confirmation *link*; a mobile app
    can't rely on the user having a browser session, so the API verifies with a
    code the app posts back instead.

    The raw code is never persisted — only its salted PBKDF2 hash (via
    ``make_password``). A 6-digit code has only 1 000 000 possible values, so
    low entropy is mitigated by: a short TTL, a hard per-code attempt cap,
    per-IP API throttling on the verify endpoint, and a per-user invalidation
    policy (issuing a new code cancels all prior unconsumed codes for that user).
    """

    CODE_TTL = timedelta(minutes=10)
    MAX_ATTEMPTS = 5

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="email_verification_codes",
    )
    code_hash = models.CharField(max_length=128, editable=False)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EmailVerificationCode for {self.user} (expires {self.expires_at})"

    @classmethod
    def issue(cls, user) -> tuple[EmailVerificationCode, str]:
        """Issue a fresh code for *user*.

        Any prior unconsumed codes for this user are invalidated so only the
        newest code is ever redeemable. Returns ``(code_row, raw_code)``; the
        raw code must be sent to the user immediately — it is never stored.
        """
        now = timezone.now()
        # Invalidate all prior unconsumed codes for this user.
        cls.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=now, updated_at=now)
        raw_code = f"{secrets.randbelow(1_000_000):06d}"
        code_row = cls.objects.create(
            user=user,
            code_hash=make_password(raw_code),
            expires_at=now + cls.CODE_TTL,
        )
        return code_row, raw_code

    @property
    def is_redeemable(self) -> bool:
        """True if this code can still be presented to verify the account."""
        if self.consumed_at is not None:
            return False
        if self.expires_at < timezone.now():
            return False
        return self.attempts < self.MAX_ATTEMPTS

    def verify(self, raw_code: str) -> bool:
        """Check *raw_code* against the stored hash.

        Returns ``False`` immediately for non-redeemable codes (expired, consumed,
        or attempts exhausted). On failure: atomically increments the attempt counter
        (prevents TOCTOU on the cap). On success: atomically marks the code consumed
        (filter+update so only one concurrent winner is possible).
        """
        if not self.is_redeemable:
            return False
        if not check_password(raw_code, self.code_hash):
            # Atomic increment so concurrent requests can't race past MAX_ATTEMPTS.
            type(self).objects.filter(pk=self.pk).update(attempts=F("attempts") + 1, updated_at=timezone.now())
            return False
        # Atomic CAS: only the first concurrent correct submission wins. A second
        # simultaneous correct submission sees 0 rows updated and returns False.
        updated = (
            type(self)
            .objects.filter(pk=self.pk, consumed_at__isnull=True)
            .update(consumed_at=timezone.now(), updated_at=timezone.now())
        )
        return bool(updated)


class AccountDeletionRequest(TimestampedModel):
    """A user-initiated account deletion, held for a grace period before execution.

    App stores require an in-app deletion path, and an immediate irreversible
    delete is a footgun — so a request starts a grace period (see
    ``core.tasks.GRACE_PERIOD_DAYS``) during which the user can cancel. The
    nightly ``sweep_due_account_deletions`` task executes anything past due.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CANCELED = "canceled", "Canceled"
        COMPLETED = "completed", "Completed"

    history = AuditlogHistoryField()

    # SET_NULL, not CASCADE: the row is the record that a deletion happened, so it
    # has to outlive the account it deleted. What remains is deliberately anonymous —
    # timestamps and a reason, with no way back to the person.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deletion_requests",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_for = models.DateTimeField(help_text="When the grace period ends and deletion executes.")
    completed_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Deletion request for {self.user or '(deleted user)'} ({self.status})"

    @property
    def is_cancelable(self) -> bool:
        """True while the request is still pending and inside its grace period."""
        return self.status == self.Status.SCHEDULED and self.scheduled_for > timezone.now()


auditlog.register(User, exclude_fields=["password", "last_login"])
auditlog.register(AppSettings)
auditlog.register(AccountDeletionRequest)
