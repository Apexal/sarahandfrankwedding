"""Guest list system of record.

The unit of invitation is the household; the unit of RSVP is (guest, event). Pending is
the absence of an `Rsvp` row. Address state is `Household.address_confirmed_at`, not
whether the address fields are blank. See `guests/CLAUDE.md` and PLAN.md section 6.
"""

import re
import secrets
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django_ckeditor_5.fields import CKEditor5Field

from core.models import TimestampedModel
from core.utils.phone import normalize_phone_number
from pages.models import WeddingSettings

# No 0/O, 1/I/L: the code is read off a printed card and typed on a phone.
INVITE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
INVITE_CODE_LENGTH = 6

_NON_WORD = re.compile(r"[\W_]+")
_NAME_SEPARATORS = re.compile(r"\s*(?:&|\+|,|\band\b)\s*", re.IGNORECASE)


def generate_invite_code() -> str:
    return "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))


def normalize_name(value: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace.

    Applied to stored names in `Guest.save()` and to typed input in the RSVP lookup, so
    both sides of a comparison go through the same function.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD.sub(" ", without_marks.casefold()).strip()


def split_names(names: str) -> list[tuple[str, str]]:
    """Split free-text names into (first, last) pairs.

    "Bob & Carol Jones" → [("Bob", "Jones"), ("Carol", "Jones")]: a name without a
    surname borrows the next surname. Best effort; the admin review is manual for a reason.
    """
    parts = [part.strip() for part in _NAME_SEPARATORS.split(names or "") if part and part.strip()]
    parsed: list[list[str]] = []
    for part in parts:
        tokens = part.split()
        if len(tokens) > 1:
            parsed.append([tokens[0], " ".join(tokens[1:])])
        else:
            parsed.append([tokens[0], ""])
    for index in range(len(parsed) - 2, -1, -1):
        if not parsed[index][1]:
            parsed[index][1] = parsed[index + 1][1]
    return [(first, last) for first, last in parsed]


class CodesFrozenError(Exception):
    """Raised when regenerating an invite code after the save-the-date has gone out."""


class Event(TimestampedModel):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=64, unique=True, help_text="Used in import files and URLs.")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    venue_name = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    description = CKEditor5Field(blank=True, config_name="extends")
    dress_code = models.CharField(max_length=100, blank=True)
    is_private = models.BooleanField(
        default=False,
        help_text="Shown on the schedule only to households invited to it (the rehearsal dinner).",
    )
    rsvp_required = models.BooleanField(default=True)
    rsvp_deadline = models.DateField(
        null=True,
        blank=True,
        help_text="Overrides the wedding-wide RSVP deadline for this event only.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta(TimestampedModel.Meta):
        ordering = ("order", "starts_at")

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.ends_at and self.starts_at and self.ends_at < self.starts_at:
            raise ValidationError({"ends_at": "The event cannot end before it starts."})

    @property
    def effective_rsvp_deadline(self):
        return self.rsvp_deadline or WeddingSettings.load().rsvp_deadline


class Household(TimestampedModel):
    """The unit of invitation: what an envelope is addressed to.

    Per-guest invitation exceptions (one member of a couple invited to the rehearsal
    dinner, the other not) are handled by splitting them into two households.
    """

    class Side(models.TextChoices):
        SARAH = "sarah", "Sarah's side"
        FRANK = "frank", "Frank's side"
        BOTH = "both", "Both"

    name = models.CharField(
        max_length=200,
        unique=True,
        help_text='As it appears on the envelope: "The Smiths", "Bob Jones & Guest".',
    )
    invite_code = models.CharField(
        max_length=INVITE_CODE_LENGTH,
        unique=True,
        editable=False,
        help_text="Generated. The token in the save-the-date link and the code on the invitation.",
    )
    side = models.CharField(max_length=8, choices=Side, default=Side.BOTH)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=16, blank=True, help_text="For texting the save-the-date link.")
    email_opt_out = models.BooleanField(default=False, help_text="Tick if they ask us to stop emailing.")
    email_bounced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when an email to this address bounces. Campaigns skip the household until cleared.",
    )

    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=64, blank=True, default="US")
    address_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when the household confirms on the save-the-date page, or by us after a call. "
            "Imported addresses are unconfirmed until then."
        ),
    )

    invited_events = models.ManyToManyField(
        Event,
        blank=True,
        related_name="households",
        help_text=(
            "Everyone in the household is invited to the same events. If one person is invited to "
            "something the others are not, split them into their own household."
        ),
    )
    responded_at = models.DateTimeField(null=True, blank=True, help_text="Set on the first RSVP submission.")
    notes = models.TextField(blank=True, help_text="Private.")

    class Meta(TimestampedModel.Meta):
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone_number(self.phone)
        if not self.invite_code:
            self.invite_code = self._unused_invite_code()
        super().save(*args, **kwargs)

    @classmethod
    def _unused_invite_code(cls) -> str:
        for _ in range(20):
            code = generate_invite_code()
            if not cls.objects.filter(invite_code=code).exists():
                return code
        raise RuntimeError("Could not find an unused invite code.")  # pragma: no cover

    def regenerate_invite_code(self) -> str:
        if WeddingSettings.load().codes_frozen:
            raise CodesFrozenError("Invite codes are frozen: they are already in save-the-date links.")
        self.invite_code = self._unused_invite_code()
        self.save(update_fields=("invite_code", "updated_at"))
        return self.invite_code

    @property
    def save_the_date_path(self) -> str:
        return f"/save-the-date/{self.invite_code}/"

    @property
    def save_the_date_url(self) -> str:
        return f"{settings.APP_BASE_URL.rstrip('/')}{self.save_the_date_path}"

    @property
    def has_address(self) -> bool:
        return bool(self.address_line1 and self.city and self.postal_code)

    @property
    def address_confirmed(self) -> bool:
        return self.address_confirmed_at is not None

    @property
    def responded(self) -> bool:
        return self.responded_at is not None

    @property
    def can_email(self) -> bool:
        return bool(self.email) and not self.email_opt_out and self.email_bounced_at is None

    def mark_address_confirmed(self, when=None) -> None:
        self.address_confirmed_at = when or timezone.now()
        self.save(update_fields=("address_confirmed_at", "updated_at"))

    @property
    def contact_names(self) -> str:
        """First names of the named guests, "Bob & Carol"; falls back to the household name."""
        names = [guest.first_name for guest in self.guests.all() if guest.first_name and not guest.is_plus_one]
        return " & ".join(names) if names else self.name

    def save_the_date_message(self, wedding: WeddingSettings | None = None) -> str:
        """The text message we send from our phones with this household's link."""
        wedding = wedding or WeddingSettings.load()
        when = wedding.wedding_date
        where = f" in {wedding.wedding_city}" if wedding.wedding_city else ""
        return (
            f"Hi {self.contact_names}! {wedding.couple_names} are getting married on "
            f"{when:%B} {when.day}, {when.year}{where}. Save the date, and please confirm your "
            f"mailing address here: {self.save_the_date_url}"
        )


class Table(TimestampedModel):
    """A reception table. Seating is reception-only: `Guest.table` is one assignment per guest."""

    name = models.CharField(max_length=100, unique=True)
    capacity = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    order = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("order", "name")

    def __str__(self) -> str:
        return self.name

    @property
    def seats_used(self) -> int:
        return self.guests.count()

    @property
    def seats_free(self) -> int:
        return self.capacity - self.seats_used


class Guest(TimestampedModel):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="guests")
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    is_plus_one = models.BooleanField(
        default=False,
        help_text='An unnamed guest ("Bob Jones & Guest"). The name is filled in when they RSVP.',
    )
    is_child = models.BooleanField(default=False)
    also_known_as = models.CharField(
        max_length=200,
        blank=True,
        help_text='Nicknames the RSVP lookup should accept, comma-separated: "Bob, Bobby".',
    )
    email = models.EmailField(blank=True, help_text="Optional. The household email is what we send to.")
    table = models.ForeignKey(
        Table,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="guests",
        help_text="Reception seat assignment.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    first_name_normalized = models.CharField(max_length=100, blank=True, editable=False)
    last_name_normalized = models.CharField(max_length=100, blank=True, editable=False, db_index=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("household__name", "sort_order", "id")

    def __str__(self) -> str:
        return self.display_name

    def clean(self):
        if not self.is_plus_one and not (self.first_name or self.last_name):
            raise ValidationError("A guest needs a name. Tick 'plus one' for an unnamed guest.")

    def save(self, *args, **kwargs):
        self.first_name_normalized = normalize_name(self.first_name)
        self.last_name_normalized = normalize_name(self.last_name)
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def display_name(self) -> str:
        return self.full_name or f"Guest of {self.household.name}"

    @property
    def aliases(self) -> list[str]:
        return [normalize_name(alias) for alias in self.also_known_as.split(",") if alias.strip()]


class Rsvp(TimestampedModel):
    """One answer per (guest, event). No row means not answered yet."""

    class Source(models.TextChoices):
        GUEST = "guest", "Guest"
        ADMIN = "admin", "Entered by us"

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name="rsvps")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rsvps")
    attending = models.BooleanField()
    responded_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=8, choices=Source, default=Source.GUEST)

    class Meta(TimestampedModel.Meta):
        ordering = ("event__order", "guest__household__name", "guest__sort_order")
        constraints = [models.UniqueConstraint(fields=("guest", "event"), name="guests_rsvp_unique_guest_event")]
        indexes = [models.Index(fields=("event", "attending"), name="guests_rsvp_event_attending")]
        verbose_name = "RSVP"
        verbose_name_plural = "RSVPs"

    def __str__(self) -> str:
        return f"{self.guest}: {'yes' if self.attending else 'no'} to {self.event}"


class RsvpQuestion(TimestampedModel):
    class Kind(models.TextChoices):
        SHORT_TEXT = "short_text", "Short text"
        LONG_TEXT = "long_text", "Long text"
        SINGLE_CHOICE = "single_choice", "Single choice"
        MULTI_CHOICE = "multi_choice", "Multiple choice"
        YES_NO = "yes_no", "Yes / no"

    class Scope(models.TextChoices):
        PER_GUEST = "per_guest", "Ask for each guest"
        PER_HOUSEHOLD = "per_household", "Ask once per household"

    CHOICE_KINDS = frozenset({Kind.SINGLE_CHOICE, Kind.MULTI_CHOICE})

    label = models.CharField(max_length=200)
    help_text = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=16, choices=Kind)
    choices = models.JSONField(
        default=list,
        blank=True,
        help_text='For the choice kinds: a list of strings, e.g. ["Beef", "Fish", "Vegetarian"].',
    )
    scope = models.CharField(max_length=16, choices=Scope, default=Scope.PER_GUEST)
    only_if_attending = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conditional_questions",
        help_text="Ask only when the guest (or someone in the household) is attending this event.",
    )
    show_on_dashboard = models.BooleanField(
        default=False,
        help_text="Roll up the answers on the dashboard (counts for choices, a list for text).",
    )
    required = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    active = models.BooleanField(default=True, help_text="Inactive questions are not asked but keep their answers.")

    class Meta(TimestampedModel.Meta):
        ordering = ("order", "id")
        verbose_name = "RSVP question"

    def __str__(self) -> str:
        return self.label

    @property
    def is_choice(self) -> bool:
        return self.kind in self.CHOICE_KINDS

    def clean(self):
        choices = self.choices or []
        if self.is_choice:
            if (
                not isinstance(choices, list)
                or len(choices) < 2
                or any(not isinstance(choice, str) or not choice.strip() for choice in choices)
            ):
                raise ValidationError({"choices": "Choice questions need a list of at least two non-empty options."})
            if len({choice.strip() for choice in choices}) != len(choices):
                raise ValidationError({"choices": "Choices must be unique."})
        elif choices:
            raise ValidationError({"choices": "Only choice questions take a list of choices."})


class RsvpAnswer(TimestampedModel):
    """`guest` is set for per-guest questions and null for per-household ones."""

    question = models.ForeignKey(RsvpQuestion, on_delete=models.CASCADE, related_name="answers")
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="rsvp_answers")
    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, null=True, blank=True, related_name="rsvp_answers")
    value = models.JSONField(help_text="A string, a list of strings, or true/false, depending on the question kind.")

    class Meta(TimestampedModel.Meta):
        ordering = ("question__order", "household__name", "guest__sort_order")
        # Two partial constraints rather than nulls_distinct=False so SQLite and Postgres behave the same.
        constraints = [
            models.UniqueConstraint(
                fields=("question", "household", "guest"),
                condition=Q(guest__isnull=False),
                name="guests_rsvpanswer_unique_per_guest",
            ),
            models.UniqueConstraint(
                fields=("question", "household"),
                condition=Q(guest__isnull=True),
                name="guests_rsvpanswer_unique_per_household",
            ),
        ]
        verbose_name = "RSVP answer"

    def __str__(self) -> str:
        who = self.guest.display_name if self.guest_id else self.household.name
        return f"{who}: {self.question.label}"

    def clean(self):
        if not self.question_id:
            return
        if self.question.scope == RsvpQuestion.Scope.PER_HOUSEHOLD:
            if self.guest_id:
                raise ValidationError({"guest": "This question is answered once per household; leave the guest blank."})
        else:
            if not self.guest_id:
                raise ValidationError({"guest": "This question is answered per guest; pick the guest."})
            if self.household_id and self.guest.household_id != self.household_id:
                raise ValidationError({"guest": "The guest does not belong to this household."})


class ContactSubmission(TimestampedModel):
    """An address submitted through the generic save-the-date page, awaiting manual review."""

    class Status(models.TextChoices):
        NEW = "new", "New"
        MERGED = "merged", "Merged"
        IGNORED = "ignored", "Ignored"

    names = models.CharField(max_length=300, help_text='As typed: "Bob & Carol Jones".')
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=16, blank=True)
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status, default=Status.NEW)
    household = models.ForeignKey(
        Household,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_submissions",
        help_text="Set when merged. Pick a household here, then use the merge action.",
    )
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.names

    def save(self, *args, **kwargs):
        self.phone = normalize_phone_number(self.phone)
        super().save(*args, **kwargs)

    @property
    def has_address(self) -> bool:
        return bool(self.address_line1 and self.city and self.postal_code)

    ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "postal_code", "country")

    def merge_into(self, household: Household, *, when=None) -> None:
        """Copy contact details onto `household`, confirm its address, and mark this submission merged."""
        if self.has_address:
            for field in self.ADDRESS_FIELDS:
                setattr(household, field, getattr(self, field) or getattr(household, field))
            household.address_confirmed_at = when or timezone.now()
        if self.email:
            household.email = self.email
        if self.phone:
            household.phone = self.phone
        if self.notes:
            household.notes = f"{household.notes}\n{self.notes}".strip()
        household.save()
        self.household = household
        self.status = self.Status.MERGED
        self.save(update_fields=("household", "status", "updated_at"))


class Campaign(TimestampedModel):
    """A templated email to an audience of households, sent in daily batches."""

    class Audience(models.TextChoices):
        ALL_WITH_EMAIL = "all_with_email", "Everyone with an email"
        MISSING_ADDRESS = "missing_address", "Address not yet confirmed"
        NON_RESPONDERS = "non_responders", "Has not RSVPed"
        MISSING_ANSWERS = "missing_answers", "Missing an answer for an invited event"
        ATTENDING = "attending", "Attending an event"
        DECLINED = "declined", "Responded, nobody attending"
        SELECTED = "selected", "Selected households"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"

    name = models.CharField(max_length=200)
    template_key = models.SlugField(max_length=64, blank=True, help_text="A messaging template key.")
    audience = models.CharField(max_length=20, choices=Audience)
    event = models.ForeignKey(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text='Required for the "attending an event" audience.',
    )
    households = models.ManyToManyField(
        Household,
        blank=True,
        related_name="campaigns",
        help_text='Used only by the "selected households" audience.',
    )
    daily_cap = models.PositiveSmallIntegerField(
        default=25,
        validators=[MinValueValidator(1)],
        help_text="Emails handed to the queue per day. Keep low while the sending domain is young.",
    )
    status = models.CharField(max_length=8, choices=Status, default=Status.DRAFT)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta(TimestampedModel.Meta):
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.audience == self.Audience.ATTENDING and not self.event_id:
            raise ValidationError({"event": "Pick the event for an 'attending' audience."})
        if self.status != self.Status.DRAFT and not self.template_key:
            raise ValidationError({"template_key": "A campaign needs a template before it can send."})

    def audience_households(self):
        """Households this campaign addresses, before per-recipient skips are recorded.

        Every audience excludes households with no email, an opt-out, or a bounce.
        """
        base = Household.objects.exclude(email="").filter(email_opt_out=False, email_bounced_at__isnull=True)
        match self.audience:
            case self.Audience.ALL_WITH_EMAIL:
                return base
            case self.Audience.MISSING_ADDRESS:
                return base.filter(address_confirmed_at__isnull=True)
            case self.Audience.NON_RESPONDERS:
                return base.filter(responded_at__isnull=True)
            case self.Audience.MISSING_ANSWERS:
                return base.filter(pk__in=_households_missing_answers(base))
            case self.Audience.ATTENDING:
                if not self.event_id:
                    return base.none()
                return base.filter(guests__rsvps__event_id=self.event_id, guests__rsvps__attending=True).distinct()
            case self.Audience.DECLINED:
                return base.filter(responded_at__isnull=False).exclude(guests__rsvps__attending=True).distinct()
            case self.Audience.SELECTED:
                return base.filter(pk__in=self.households.values("pk"))
        return base.none()  # pragma: no cover


def _households_missing_answers(households) -> list[int]:
    """Ids of households with a guest lacking an Rsvp for an invited, rsvp-required event.

    Done in Python: the list is a few hundred rows and a nested EXISTS over an M2M is
    not worth the query.
    """
    missing: list[int] = []
    queryset = households.prefetch_related("invited_events", "guests__rsvps")
    for household in queryset:
        required_event_ids = {event.pk for event in household.invited_events.all() if event.rsvp_required}
        if not required_event_ids:
            continue
        for guest in household.guests.all():
            answered = {rsvp.event_id for rsvp in guest.rsvps.all()}
            if required_event_ids - answered:
                missing.append(household.pk)
                break
    return missing


class CampaignRecipient(TimestampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Handed to the email queue"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="campaign_recipients")
    outbound_email = models.ForeignKey(
        "messaging.OutboundEmail",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    status = models.CharField(max_length=8, choices=Status, default=Status.QUEUED)
    error = models.TextField(blank=True)

    class Meta(TimestampedModel.Meta):
        ordering = ("campaign", "household__name")
        constraints = [
            models.UniqueConstraint(fields=("campaign", "household"), name="guests_campaignrecipient_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.household} in {self.campaign} ({self.get_status_display()})"
