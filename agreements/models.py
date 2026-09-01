from auditlog.models import AuditlogHistoryField
from auditlog.registry import auditlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import TimestampedModel

# Keys that the agreements URLconf already uses for its own routes. An Agreement
# with one of these keys would have an unreachable public page.
RESERVED_AGREEMENT_KEYS = frozenset({"pending", "history", "versions"})


def validate_agreement_key(value: str) -> None:
    if value in RESERVED_AGREEMENT_KEYS:
        raise ValidationError(
            "%(value)s is reserved by the agreements URLs — pick another key.",
            params={"value": value},
        )


class Agreement(TimestampedModel):
    """A category of legal agreement (e.g. Terms of Service, Privacy Policy).

    `current_version` is the version users must currently accept. Advance it by
    creating a new AgreementVersion and publishing it (admin action or
    `AgreementVersion.publish()`) — never by editing text in place.
    """

    history = AuditlogHistoryField()
    key = models.SlugField(
        max_length=64,
        unique=True,
        validators=[validate_agreement_key],
        help_text="Stable identifier used in code and in the public URL, e.g. 'terms-of-service'.",
    )
    name = models.CharField(max_length=200)
    required = models.BooleanField(
        default=True,
        help_text="Block access to the site until the current version is accepted. Needs 'requires reacceptance' on too.",
    )
    requires_reacceptance = models.BooleanField(
        default=True,
        help_text=(
            "Uncheck for notice-only agreements. Unchecking stops this agreement blocking "
            "access at all — not just on new versions — so no acceptance is ever recorded."
        ),
    )
    current_version = models.ForeignKey(
        "AgreementVersion",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
        help_text="The version currently in force. Set via the admin 'Publish' action.",
    )

    class Meta(TimestampedModel.Meta):
        ordering = ("name",)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("agreements:current", args=[self.key])


class AgreementVersion(TimestampedModel):
    """An immutable snapshot of an Agreement's legal text.

    Never edit `content_markdown` after publishing — create a new version instead.
    `on_delete=PROTECT` on UserAgreement means acceptance records can never be
    orphaned by deleting the version someone accepted.
    """

    history = AuditlogHistoryField()
    agreement = models.ForeignKey(Agreement, related_name="versions", on_delete=models.PROTECT)
    version = models.PositiveIntegerField(
        blank=True,
        help_text="Leave blank to assign the next number for this agreement automatically.",
    )
    content_markdown = models.TextField(help_text="Full legal text, written in Markdown.")
    effective_date = models.DateField()
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="published_agreement_versions",
    )
    published_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta(TimestampedModel.Meta):
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(fields=("agreement", "version"), name="unique_version_per_agreement"),
        ]

    def __str__(self):
        return f"{self.agreement.name} v{self.version}"

    def get_absolute_url(self):
        return reverse("agreements:version_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.version:
            # Numbering is per-agreement and starts at 1. Two admins adding a version
            # at the same instant would collide — unique_version_per_agreement turns
            # that into an IntegrityError rather than a silently duplicated number.
            highest = AgreementVersion.objects.filter(agreement_id=self.agreement_id).aggregate(
                highest=models.Max("version")
            )["highest"]
            self.version = (highest or 0) + 1
        return super().save(*args, **kwargs)

    @property
    def is_current(self) -> bool:
        return self.agreement.current_version_id == self.pk

    def publish(self, *, published_by=None):
        """Make this the current version of its agreement.

        Saved through the ORM (rather than a bulk update) so auditlog records both
        sides of the change and the post_save receiver clears the required-version cache.
        """
        with transaction.atomic():
            self.published_by = published_by or self.published_by
            self.published_at = timezone.now()
            self.save(update_fields=["published_by", "published_at", "updated_at"])
            self.agreement.current_version = self
            self.agreement.save(update_fields=["current_version", "updated_at"])


class UserAgreement(TimestampedModel):
    """Immutable record of a user accepting a specific AgreementVersion.

    Rows are never modified or deleted — they are the audit trail proving what a
    user agreed to and when. `on_delete=PROTECT` on both FKs enforces that at the
    database level, and `created_at` is the acceptance timestamp.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="user_agreements",
        on_delete=models.PROTECT,
    )
    agreement_version = models.ForeignKey(
        AgreementVersion,
        related_name="user_agreements",
        on_delete=models.PROTECT,
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("user", "agreement_version"), name="unique_acceptance_per_user"),
        ]

    def __str__(self):
        return f"{self.user} → {self.agreement_version}"


auditlog.register(Agreement)
auditlog.register(AgreementVersion)
