from typing import TYPE_CHECKING

from auditlog.models import AuditlogHistoryField
from auditlog.registry import auditlog
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from core.models import TimestampedModel
from messaging.template_definitions import (
    get_allowed_template_keys,
    get_template_definition,
)


class EmailTemplateVersion(TimestampedModel):
    """Stored HTML template content keyed by hardcoded template key."""

    history = AuditlogHistoryField()
    template_key = models.SlugField(
        max_length=120,
        unique=True,
        db_index=True,
        verbose_name="Template key",
        help_text="Hardcoded template type key.",
    )
    subject_template = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Subject template",
        help_text="Subject template rendered using Django template syntax.",
    )
    html_body_template = models.TextField(
        verbose_name="HTML body template",
        blank=True,
        help_text="HTML template body rendered using Django template syntax.",
    )

    if TYPE_CHECKING:
        outbound_emails: models.QuerySet[OutboundEmail]

    class Meta(TimestampedModel.Meta):
        verbose_name = "Email template HTML"
        verbose_name_plural = "Email template HTML"
        ordering = ("template_key",)

    @classmethod
    def template_key_choices(cls):
        choices = []
        for key in get_allowed_template_keys():
            definition = get_template_definition(key)
            choices.append((key, definition.name if definition else key))
        return choices

    @property
    def template_name(self):
        definition = get_template_definition(self.template_key)
        if definition is None:
            return self.template_key
        return definition.name

    def clean(self):
        super().clean()
        if self.template_key:
            self.template_key = slugify(self.template_key)

        definition = get_template_definition(self.template_key)
        if definition is None:
            allowed_keys = ", ".join(get_allowed_template_keys())
            raise ValidationError(
                {
                    "template_key": (
                        f"Template key '{self.template_key}' is not supported. Allowed keys: {allowed_keys}."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.template_key} HTML"


class OutboundEmail(TimestampedModel):
    """Audit record for an outbound email send attempt."""

    class DeliveryStatus(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    to_email = models.EmailField(
        db_index=True,
        verbose_name="Recipient email",
        help_text="Primary recipient email address.",
    )
    from_email = models.EmailField(
        blank=True,
        verbose_name="Sender email",
        help_text="Email address used in the From header.",
    )
    reply_to_email = models.EmailField(
        blank=True,
        verbose_name="Reply-to email",
        help_text="Reply-to email address applied at send time, if set.",
    )
    subject_rendered = models.CharField(
        max_length=255,
        verbose_name="Rendered subject",
        help_text="Final rendered subject line that was sent.",
    )
    body_html_rendered = models.TextField(
        blank=True,
        verbose_name="Rendered HTML body",
        help_text="Final rendered HTML body sent to the provider.",
    )
    body_text_rendered = models.TextField(
        blank=True,
        verbose_name="Rendered text body",
        help_text="Final rendered text body sent to the provider.",
    )
    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.QUEUED,
        db_index=True,
        verbose_name="Delivery status",
        help_text="Current delivery state reported by the sending pipeline.",
    )
    error_message = models.TextField(
        blank=True,
        verbose_name="Error message",
        help_text="Error details when sending fails.",
    )
    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Sent at",
        help_text="Timestamp when the email was successfully handed to the provider.",
    )
    send_context = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Send context",
        help_text="Context payload used to render this email.",
    )
    template_key = models.SlugField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Template key",
        help_text="Hardcoded template key used to resolve the HTML template.",
    )
    template_version = models.ForeignKey(
        EmailTemplateVersion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outbound_emails",
        verbose_name="Template HTML",
        help_text="HTML template row used when this email was rendered.",
    )

    class Meta(TimestampedModel.Meta):
        verbose_name = "Outbound email"
        verbose_name_plural = "Outbound emails"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("to_email", "-created_at"),
                name="messaging_to_recent_idx",
            ),
            models.Index(
                fields=("status", "-created_at"),
                name="messaging_status_recent_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.to_email} ({self.status})"


auditlog.register(EmailTemplateVersion)
