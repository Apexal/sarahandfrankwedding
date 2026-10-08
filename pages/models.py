"""Wedding site content: the `WeddingSettings` singleton and the prose Sarah edits in the admin.

`guests` imports this module (for `WeddingSettings`), so nothing here may import
`guests.models` at module level.
"""

import hashlib
import re
from datetime import date

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.utils import timezone
from django_ckeditor_5.fields import CKEditor5Field

from core.constants import COMPANY, WEDDING_DATE
from core.models import TimestampedModel

# Fixed routes in the project URLconf. A Page with one of these slugs would either be
# shadowed by the route or shadow it, depending on URL order, so the slug validator
# rejects them. Add to this set whenever a fixed route is added.
RESERVED_PAGE_SLUGS = frozenset(
    {
        "rsvp",
        "save-the-date",
        "schedule",
        "welcome",
        "faq",
        "registry",
        "photos",
        "manage",
        "admin",
        "accounts",
        "health",
        "anymail",
        "ckeditor5",
        "static",
        "media",
    }
)


def default_couple_names() -> str:
    return COMPANY["name"]


def default_wedding_date() -> date:
    return date.fromisoformat(WEDDING_DATE)


def validate_page_slug(value: str) -> None:
    if value in RESERVED_PAGE_SLUGS:
        raise ValidationError(
            "%(value)s is a fixed route on the site — pick another slug.",
            params={"value": value},
        )


def validate_registry_links(value) -> None:
    """`registry_links` is a list of {"label": ..., "url": ...} objects."""
    if not isinstance(value, list):
        raise ValidationError("Registry links must be a list of {label, url} entries.")
    url_validator = URLValidator(schemes=["http", "https"])
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != {"label", "url"}:
            raise ValidationError(f"Registry link {index} must have exactly a 'label' and a 'url'.")
        if not isinstance(item["label"], str) or not item["label"].strip():
            raise ValidationError(f"Registry link {index} needs a label.")
        if not isinstance(item["url"], str):
            raise ValidationError(f"Registry link {index} needs a URL.")
        url_validator(item["url"])


class WeddingSettings(TimestampedModel):
    """Singleton holding the wedding's editable facts and switches.

    `save()` forces pk=1 and busts the cache, `load()` reads through the cache. Read it
    via `WeddingSettings.load()` everywhere; `core.account_adapter.SiteIdentity` reads
    `couple_names` from it for allauth's emails.
    """

    CACHE_KEY = "wedding_settings"
    couple_names = models.CharField(max_length=100, default=default_couple_names)
    wedding_date = models.DateField(default=default_wedding_date)
    wedding_city = models.CharField(
        max_length=100,
        blank=True,
        help_text='Shown on the save-the-date, e.g. "Chicago, Illinois". The only location detail it reveals.',
    )
    contact_email = models.EmailField(
        blank=True,
        help_text="Shown in the footer, used as Reply-To on guest emails, and receives internal notices.",
    )

    site_password = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Blank turns the gate off. Leave it blank until the invitations mail; it is printed on them, "
            "so it cannot change afterwards. Compared case-insensitively."
        ),
    )
    codes_frozen = models.BooleanField(
        default=False,
        help_text=(
            "Tick the day the save-the-date goes out. Invite codes are in every link and on every invitation "
            "from then on, so regenerating one is disabled."
        ),
    )

    save_the_date_open = models.BooleanField(
        default=False,
        help_text="Makes the generic /save-the-date/ page live. Personalized links work regardless.",
    )
    save_the_date_message = CKEditor5Field(
        blank=True,
        config_name="default",
        help_text="The paragraph on the save-the-date page, above the address form.",
    )

    rsvp_open = models.BooleanField(default=False, help_text="Flip on the day the first invitations mail.")
    rsvp_deadline = models.DateField(
        null=True,
        blank=True,
        help_text="The date printed on the RSVP card. The form closes at the end of that day. Events may override it.",
    )
    rsvp_intro = CKEditor5Field(blank=True, config_name="default", help_text="Shown above the name lookup.")
    rsvp_closed_message = CKEditor5Field(
        blank=True,
        config_name="default",
        help_text="Shown when RSVPs are closed or past the deadline.",
    )

    photo_album_url = models.URLField(
        blank=True,
        help_text="The shared Google Photos album link. Blank hides the Photos page and nav item.",
    )
    photo_album_note = models.CharField(
        max_length=200,
        blank=True,
        help_text='Shown under the album button, e.g. "You\'ll need a Google account to add photos."',
    )
    registry_links = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_registry_links],
        help_text='A list of {"label": "...", "url": "https://..."} entries.',
    )

    class Meta(TimestampedModel.Meta):
        verbose_name = "Wedding Settings"
        verbose_name_plural = "Wedding Settings"

    def __str__(self) -> str:
        return "Wedding Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        if self.created_at is None:
            # A fresh instance saved over the existing row must not null out created_at.
            self.created_at = type(self).objects.filter(pk=1).values_list("created_at", flat=True).first()
        super().save(*args, **kwargs)
        cache.delete(self.CACHE_KEY)

    @classmethod
    def load(cls) -> WeddingSettings:
        settings = cache.get(cls.CACHE_KEY)
        if settings is None:
            settings, _ = cls.objects.get_or_create(pk=1)
            cache.set(cls.CACHE_KEY, settings)
        return settings

    @staticmethod
    def normalize_password(value: str) -> str:
        """Passwords are typed from a printed card, so case and stray spaces must not matter."""
        return (value or "").strip().casefold()

    @property
    def gate_enabled(self) -> bool:
        return bool(self.site_password)

    @property
    def monogram(self) -> str:
        """The couple's initials for the envelope seal and stamp: "Sarah & Frank" -> "S & F"."""
        names = [name for name in re.split(r"\s+(?:&|and)\s+|\s*&\s*", self.couple_names.strip()) if name]
        return " & ".join(name[0].upper() for name in names)

    @property
    def site_password_fingerprint(self) -> str:
        """What the gate stores in the session. Changing the password changes this, which re-locks everyone."""
        if not self.site_password:
            return ""
        return hashlib.sha256(self.normalize_password(self.site_password).encode()).hexdigest()[:16]

    def check_site_password(self, candidate: str) -> bool:
        import secrets

        if not self.gate_enabled:
            return True
        return secrets.compare_digest(
            self.normalize_password(candidate).encode(),
            self.normalize_password(self.site_password).encode(),
        )

    def rsvp_is_open(self, event=None, *, today: date | None = None) -> bool:
        """The one place open/closed is decided.

        `event` may carry its own `rsvp_deadline` (a date or None) which overrides the
        wedding-wide one. With no deadline anywhere, open means open.
        """
        if not self.rsvp_open:
            return False
        deadline = getattr(event, "rsvp_deadline", None) or self.rsvp_deadline
        if deadline is None:
            return True
        return (today or timezone.localdate()) <= deadline


class Page(TimestampedModel):
    """A prose page served at /<slug>/ by the catch-all route, edited in the admin."""

    slug = models.SlugField(
        max_length=64,
        unique=True,
        validators=[validate_page_slug],
        help_text="The URL: /<slug>/. Cannot be one of the fixed routes.",
    )
    title = models.CharField(max_length=200)
    body = CKEditor5Field(blank=True, config_name="extends")
    nav_label = models.CharField(max_length=50, blank=True, help_text="Shown in the nav; defaults to the title.")
    nav_order = models.PositiveSmallIntegerField(default=0)
    show_in_nav = models.BooleanField(default=True)
    published = models.BooleanField(default=False, help_text="Unpublished pages 404 for guests.")

    class Meta(TimestampedModel.Meta):
        ordering = ("nav_order", "title")

    def __str__(self) -> str:
        return self.title

    @property
    def nav_text(self) -> str:
        return self.nav_label or self.title


class FaqItem(TimestampedModel):
    question = models.CharField(max_length=300)
    answer = CKEditor5Field(blank=True, config_name="default")
    order = models.PositiveSmallIntegerField(default=0)
    published = models.BooleanField(default=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("order", "id")
        verbose_name = "FAQ item"
        verbose_name_plural = "FAQ items"

    def __str__(self) -> str:
        return self.question
