from django import forms
from django.contrib import admin, messages
from django.db.models import BooleanField, Count, OuterRef, Q, Subquery
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from import_export.admin import ExportMixin, ImportExportMixin

from guests.campaigns import CampaignError, start_campaign
from guests.exports import (
    INVITE_CODES_HEADER,
    SAVE_THE_DATE_LINKS_HEADER,
    csv_response,
    invite_codes_rows,
    save_the_date_links_rows,
)
from guests.models import (
    Campaign,
    CampaignRecipient,
    CodesFrozenError,
    ContactSubmission,
    Event,
    Guest,
    Household,
    Rsvp,
    RsvpAnswer,
    RsvpQuestion,
    Table,
    split_names,
)
from guests.resources import GuestResource, HouseholdResource
from messaging.template_definitions import get_allowed_template_keys
from pages.models import WeddingSettings


class _IsSetFilter(admin.SimpleListFilter):
    """Yes / No filter on whether a nullable (or blank) field has a value."""

    field_name = ""
    blank_string = False

    def lookups(self, request, model_admin):
        return (("yes", "Yes"), ("no", "No"))

    def queryset(self, request, queryset):
        if self.value() not in ("yes", "no"):
            return queryset
        wants_set = self.value() == "yes"
        if self.blank_string:
            return queryset.exclude(**{self.field_name: ""}) if wants_set else queryset.filter(**{self.field_name: ""})
        return queryset.filter(**{f"{self.field_name}__isnull": not wants_set})


def is_set_filter(field_name: str, title: str, *, blank_string: bool = False) -> type[_IsSetFilter]:
    return type(
        f"{field_name.title().replace('_', '')}Filter",
        (_IsSetFilter,),
        {"title": title, "parameter_name": field_name, "field_name": field_name, "blank_string": blank_string},
    )


AddressConfirmedFilter = is_set_filter("address_confirmed_at", "address confirmed")
RespondedFilter = is_set_filter("responded_at", "responded")
HasEmailFilter = is_set_filter("email", "has email", blank_string=True)
HasPhoneFilter = is_set_filter("phone", "has phone", blank_string=True)
BouncedFilter = is_set_filter("email_bounced_at", "email bounced")


# --- Household -------------------------------------------------------------------------


class GuestInline(admin.TabularInline):
    model = Guest
    fields = ("first_name", "last_name", "is_plus_one", "is_child", "also_known_as", "email", "sort_order")
    extra = 1
    ordering = ("sort_order", "id")


@admin.register(Household)
class HouseholdAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [HouseholdResource]
    inlines = [GuestInline]
    filter_horizontal = ("invited_events",)
    list_display = (
        "name",
        "side",
        "guest_count",
        "invited_events_display",
        "address_confirmed",
        "responded",
        "has_email",
        "has_phone",
        "bounced",
        "save_the_date_link",
    )
    list_filter = (
        "side",
        AddressConfirmedFilter,
        RespondedFilter,
        "invited_events",
        HasEmailFilter,
        HasPhoneFilter,
        BouncedFilter,
    )
    search_fields = ("name", "invite_code", "guests__first_name", "guests__last_name")
    readonly_fields = ("invite_code", "save_the_date_link")
    fieldsets = (
        (None, {"fields": ("name", "side", "invite_code", "save_the_date_link", "invited_events", "notes")}),
        ("Contact", {"fields": ("email", "phone", "email_opt_out", "email_bounced_at")}),
        (
            "Mailing address",
            {
                "fields": (
                    "address_line1",
                    "address_line2",
                    ("city", "state", "postal_code"),
                    "country",
                    "address_confirmed_at",
                )
            },
        ),
        ("RSVP", {"fields": ("responded_at",), "classes": ("collapse",)}),
    )
    actions = (
        "send_campaign_to_selected",
        "mark_address_confirmed",
        "regenerate_invite_codes",
        "export_invite_codes_csv",
        "export_save_the_date_links_csv",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(guest_count=Count("guests", distinct=True))
            .prefetch_related("invited_events", "guests")
        )

    @admin.display(description="Guests", ordering="guest_count")
    def guest_count(self, obj):
        return obj.guest_count

    @admin.display(description="Invited to")
    def invited_events_display(self, obj):
        return ", ".join(event.name for event in obj.invited_events.all()) or "—"

    @admin.display(description="Address", boolean=True, ordering="address_confirmed_at")
    def address_confirmed(self, obj):
        return obj.address_confirmed

    @admin.display(description="Responded", boolean=True, ordering="responded_at")
    def responded(self, obj):
        return obj.responded

    @admin.display(description="Email", boolean=True)
    def has_email(self, obj):
        return bool(obj.email)

    @admin.display(description="Phone", boolean=True)
    def has_phone(self, obj):
        return bool(obj.phone)

    @admin.display(description="Bounced", boolean=True)
    def bounced(self, obj):
        return obj.email_bounced_at is not None

    @admin.display(description="Save-the-date link")
    def save_the_date_link(self, obj):
        if not obj.pk:
            return "Saved after the household is created."
        return format_html('<code style="user-select: all">{}</code>', obj.save_the_date_url)

    @admin.action(description="Start a campaign to the selected households")
    def send_campaign_to_selected(self, request, queryset):
        campaign = Campaign.objects.create(
            name=f"Selected households {timezone.localdate():%Y-%m-%d}",
            audience=Campaign.Audience.SELECTED,
            created_by=request.user,
        )
        campaign.households.set(queryset)
        self.message_user(request, f"Draft campaign created for {queryset.count()} household(s). Pick a template.")
        return redirect("admin:guests_campaign_change", campaign.pk)

    @admin.action(description="Mark address confirmed (after a call)")
    def mark_address_confirmed(self, request, queryset):
        now = timezone.now()
        updated = 0
        for household in queryset:
            if not household.has_address:
                self.message_user(request, f"{household} has no address to confirm.", level=messages.WARNING)
                continue
            household.mark_address_confirmed(when=now)
            updated += 1
        self.message_user(request, f"Marked {updated} address(es) confirmed.")

    @admin.action(description="Regenerate invite codes")
    def regenerate_invite_codes(self, request, queryset):
        if WeddingSettings.load().codes_frozen:
            self.message_user(
                request,
                "Invite codes are frozen: they are already in save-the-date links and on invitations.",
                level=messages.ERROR,
            )
            return
        for household in queryset:
            try:
                household.regenerate_invite_code()
            except CodesFrozenError as exc:  # pragma: no cover - guarded above
                self.message_user(request, str(exc), level=messages.ERROR)
                return
        self.message_user(request, f"Regenerated {queryset.count()} invite code(s).")

    @admin.action(description="Export invite codes CSV (for the printer)")
    def export_invite_codes_csv(self, request, queryset):
        return csv_response("invite-codes.csv", INVITE_CODES_HEADER, invite_codes_rows(queryset))

    @admin.action(description="Export save-the-date links CSV (for texting)")
    def export_save_the_date_links_csv(self, request, queryset):
        return csv_response("save-the-date-links.csv", SAVE_THE_DATE_LINKS_HEADER, save_the_date_links_rows(queryset))


# --- Guest -----------------------------------------------------------------------------


class AttendingEventFilter(admin.SimpleListFilter):
    title = "attending"
    parameter_name = "attending"

    def lookups(self, request, model_admin):
        return [(str(event.pk), event.name) for event in Event.objects.filter(rsvp_required=True)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(rsvps__event_id=self.value(), rsvps__attending=True)
        return queryset


def _event_column(event: Event):
    attr = f"rsvp_{event.pk}"

    def column(obj):
        return getattr(obj, attr)

    column.__name__ = attr
    column.short_description = event.name
    column.boolean = True
    column.admin_order_field = attr
    return column


@admin.register(Guest)
class GuestAdmin(ImportExportMixin, admin.ModelAdmin):
    """Flat list with one yes / no / pending column per RSVP-required event.

    Seats are assigned here: filter to guests attending the reception and pick a table
    in the dropdown for each. Import is the spreadsheet import (see guests/resources.py).
    """

    resource_classes = [GuestResource]
    list_display = ("display_name", "household_link", "is_plus_one", "is_child", "table")
    list_editable = ("table",)
    list_filter = ("household__side", "is_plus_one", "is_child", AttendingEventFilter, "table")
    search_fields = ("first_name", "last_name", "also_known_as", "household__name")
    autocomplete_fields = ("household", "table")
    list_select_related = ("household", "table")
    list_per_page = 200
    fields = (
        "household",
        ("first_name", "last_name"),
        ("is_plus_one", "is_child"),
        "also_known_as",
        "email",
        "table",
        "sort_order",
    )

    def _events(self, request):
        if not hasattr(request, "_guest_admin_events"):
            request._guest_admin_events = list(Event.objects.filter(rsvp_required=True))
        return request._guest_admin_events

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        for event in self._events(request):
            queryset = queryset.annotate(
                **{
                    f"rsvp_{event.pk}": Subquery(
                        Rsvp.objects.filter(guest=OuterRef("pk"), event=event).values("attending")[:1],
                        output_field=BooleanField(null=True),
                    )
                }
            )
        return queryset

    def get_list_display(self, request):
        return (*self.list_display, *(_event_column(event) for event in self._events(request)))

    @admin.display(description="Guest", ordering="last_name")
    def display_name(self, obj):
        return obj.display_name

    @admin.display(description="Household", ordering="household__name")
    def household_link(self, obj):
        url = reverse("admin:guests_household_change", args=[obj.household_id])
        return format_html('<a href="{}">{}</a>', url, obj.household.name)


# --- Event, Table ----------------------------------------------------------------------


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "starts_at",
        "venue_name",
        "is_private",
        "rsvp_required",
        "rsvp_deadline",
        "household_count",
        "order",
    )
    list_editable = ("order",)
    list_filter = ("is_private", "rsvp_required")
    search_fields = ("name", "venue_name")
    prepopulated_fields = {"slug": ("name",)}
    fieldsets = (
        (None, {"fields": ("name", "slug", "order")}),
        ("When and where", {"fields": (("starts_at", "ends_at"), "venue_name", "address", "dress_code")}),
        ("Visibility and RSVP", {"fields": ("is_private", "rsvp_required", "rsvp_deadline")}),
        ("Description", {"fields": ("description",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(household_count=Count("households", distinct=True))

    @admin.display(description="Households invited", ordering="household_count")
    def household_count(self, obj):
        return obj.household_count


class SeatedGuestInline(admin.TabularInline):
    model = Guest
    fields = ("seated_guest", "household", "is_child")
    readonly_fields = ("seated_guest", "household", "is_child")
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name = "seated guest"
    verbose_name_plural = "seated guests (assign seats from the guest list)"

    @admin.display(description="Guest")
    def seated_guest(self, obj):
        return obj.display_name

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ("name", "capacity", "seats_used", "seats_free", "order")
    list_editable = ("order",)
    search_fields = ("name",)
    inlines = [SeatedGuestInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(seat_count=Count("guests"))

    @admin.display(description="Seated", ordering="seat_count")
    def seats_used(self, obj):
        return obj.seat_count

    @admin.display(description="Free")
    def seats_free(self, obj):
        return obj.capacity - obj.seat_count


# --- RSVP ------------------------------------------------------------------------------


@admin.register(Rsvp)
class RsvpAdmin(admin.ModelAdmin):
    """Read-mostly. Saving here records `source=admin` (a phone RSVP entered by us)."""

    list_display = ("guest", "household", "event", "attending", "source", "responded_at")
    list_filter = ("event", "attending", "source")
    search_fields = ("guest__first_name", "guest__last_name", "guest__household__name")
    autocomplete_fields = ("guest", "event")
    list_select_related = ("guest__household", "event")
    date_hierarchy = "responded_at"
    readonly_fields = ("source",)

    @admin.display(description="Household", ordering="guest__household__name")
    def household(self, obj):
        return obj.guest.household

    def save_model(self, request, obj, form, change):
        obj.source = Rsvp.Source.ADMIN
        super().save_model(request, obj, form, change)


@admin.register(RsvpQuestion)
class RsvpQuestionAdmin(admin.ModelAdmin):
    list_display = ("label", "kind", "scope", "only_if_attending", "required", "show_on_dashboard", "active", "order")
    list_editable = ("required", "show_on_dashboard", "active", "order")
    list_filter = ("kind", "scope", "active", "show_on_dashboard")
    search_fields = ("label",)
    fields = (
        "label",
        "help_text",
        ("kind", "scope"),
        "choices",
        "only_if_attending",
        ("required", "active"),
        "show_on_dashboard",
        "order",
    )


@admin.register(RsvpAnswer)
class RsvpAnswerAdmin(admin.ModelAdmin):
    list_display = ("question", "household", "guest", "value", "updated_at")
    list_filter = ("question",)
    search_fields = ("household__name", "guest__first_name", "guest__last_name")
    autocomplete_fields = ("question", "household", "guest")
    list_select_related = ("question", "household", "guest")


# --- Contact submissions ---------------------------------------------------------------


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ("names", "email", "phone", "city", "status", "household", "created_at")
    list_filter = ("status",)
    search_fields = ("names", "email", "phone", "city")
    autocomplete_fields = ("household",)
    readonly_fields = ("ip", "created_at", "updated_at")
    date_hierarchy = "created_at"
    actions = ("create_household_from_submission", "merge_into_linked_household", "mark_ignored")
    fieldsets = (
        (None, {"fields": ("names", "status", "household")}),
        ("Contact", {"fields": ("email", "phone")}),
        ("Address", {"fields": ("address_line1", "address_line2", ("city", "state", "postal_code"), "country")}),
        ("Notes", {"fields": ("notes", "ip", "created_at", "updated_at")}),
    )

    @admin.action(description="Create a household from each submission")
    def create_household_from_submission(self, request, queryset):
        created = 0
        for submission in queryset.filter(status=ContactSubmission.Status.NEW):
            name = submission.names.strip()
            if Household.objects.filter(name=name).exists():
                self.message_user(
                    request,
                    f'A household named "{name}" already exists; link it and use "Merge into linked household".',
                    level=messages.WARNING,
                )
                continue
            household = Household.objects.create(name=name)
            for order, (first, last) in enumerate(split_names(name)):
                Guest.objects.create(household=household, first_name=first, last_name=last, sort_order=order)
            submission.merge_into(household)
            created += 1
        self.message_user(request, f"Created {created} household(s).")

    @admin.action(description="Merge into the linked household")
    def merge_into_linked_household(self, request, queryset):
        merged = 0
        for submission in queryset.filter(status=ContactSubmission.Status.NEW):
            if not submission.household_id:
                self.message_user(
                    request,
                    f'"{submission}" has no household linked; open it and pick one first.',
                    level=messages.WARNING,
                )
                continue
            submission.merge_into(submission.household)
            merged += 1
        self.message_user(request, f"Merged {merged} submission(s).")

    @admin.action(description="Mark ignored")
    def mark_ignored(self, request, queryset):
        count = queryset.update(status=ContactSubmission.Status.IGNORED)
        self.message_user(request, f"Ignored {count} submission(s).")


# --- Campaigns -------------------------------------------------------------------------


class CampaignAdminForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = ("name", "template_key", "daily_cap", "audience", "event", "households")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keys = get_allowed_template_keys()
        if keys:
            self.fields["template_key"] = forms.ChoiceField(
                choices=[("", "---------"), *((key, key) for key in keys)],
                required=False,
                help_text=Campaign._meta.get_field("template_key").help_text,
            )


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    fields = ("household", "status", "outbound_email", "error")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    form = CampaignAdminForm
    list_display = ("name", "template_key", "audience", "event", "status", "daily_cap", "recipient_summary", "sent_at")
    list_filter = ("status", "audience")
    search_fields = ("name",)
    filter_horizontal = ("households",)
    readonly_fields = ("status", "sent_at", "created_by", "audience_size")
    inlines = [CampaignRecipientInline]
    actions = ("send_campaigns",)
    fieldsets = (
        (
            None,
            {
                "fields": ("name", "template_key", "daily_cap"),
                "description": (
                    "How sending works: pick a template and an audience, save, then tick the campaign in the "
                    "list and run “Send selected campaigns”. The first batch of up to the daily cap is handed "
                    "to the email queue right away; every morning at 09:00 the next batch goes out until nobody "
                    "is left, and the campaign is marked sent. Each recipient links to its outbound email, "
                    "which carries the delivery status."
                ),
            },
        ),
        ("Audience", {"fields": ("audience", "event", "households", "audience_size")}),
        ("Progress", {"fields": ("status", "sent_at", "created_by")}),
    )

    @admin.action(description="Send selected campaigns")
    def send_campaigns(self, request, queryset):
        started = 0
        for campaign in queryset:
            try:
                start_campaign(campaign)
            except CampaignError as exc:
                self.message_user(request, f"{campaign}: {exc}", level=messages.WARNING)
                continue
            started += 1
        if started:
            self.message_user(
                request,
                f"Started {started} campaign(s). The first batch is queued; the rest go out at 09:00 each morning.",
            )

    def get_queryset(self, request):
        counts = {
            f"count_{status}": Count("recipients", filter=Q(recipients__status=status), distinct=True)
            for status in CampaignRecipient.Status.values
        }
        return super().get_queryset(request).annotate(**counts)

    @admin.display(description="Recipients")
    def recipient_summary(self, obj):
        parts = [
            f"{getattr(obj, f'count_{status}')} {label.lower()}"
            for status, label in CampaignRecipient.Status.choices
            if getattr(obj, f"count_{status}")
        ]
        return " · ".join(parts) or "—"

    @admin.display(description="Households in audience now")
    def audience_size(self, obj):
        if not obj.pk:
            return "Save the campaign to see the count."
        return obj.audience_households().count()

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ("campaign", "household", "status", "outbound_email", "updated_at")
    list_filter = ("status", "campaign")
    search_fields = ("household__name",)
    list_select_related = ("campaign", "household", "outbound_email")
    readonly_fields = ("campaign", "household", "outbound_email", "status", "error")

    def has_add_permission(self, request):
        return False
