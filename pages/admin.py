from django.contrib import admin
from django.shortcuts import redirect

from pages.models import FaqItem, Page, WeddingSettings


@admin.register(WeddingSettings)
class WeddingSettingsAdmin(admin.ModelAdmin):
    """Singleton admin: the changelist redirects straight to the one row."""

    readonly_fields = ("gate_status",)
    fieldsets = (
        ("The wedding", {"fields": ("couple_names", "wedding_date", "wedding_city", "contact_email")}),
        (
            "Save the date",
            {
                "fields": ("save_the_date_open", "save_the_date_message", "codes_frozen"),
                "description": (
                    "Personalized /save-the-date/&lt;code&gt;/ links always work. The open switch controls only "
                    "the generic page used in group chats."
                ),
            },
        ),
        (
            "Site access",
            {
                "fields": ("site_password", "gate_status"),
                "description": (
                    "Leave the password blank until the invitations mail. Anyone arriving from a save-the-date "
                    "link is let in without it."
                ),
            },
        ),
        ("RSVP", {"fields": ("rsvp_open", "rsvp_deadline", "rsvp_intro", "rsvp_closed_message")}),
        ("Photos and registry", {"fields": ("photo_album_url", "photo_album_note", "registry_links")}),
    )

    @admin.display(description="Gate")
    def gate_status(self, obj):
        return "On — guests need the password" if obj.gate_enabled else "Off — the site is open"

    def has_add_permission(self, request):
        return not WeddingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = WeddingSettings.load()
        return redirect("admin:pages_weddingsettings_change", obj.pk)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "published", "show_in_nav", "nav_order", "updated_at")
    list_editable = ("published", "show_in_nav", "nav_order")
    list_filter = ("published", "show_in_nav")
    search_fields = ("title", "slug")
    prepopulated_fields = {"slug": ("title",)}
    fieldsets = (
        (None, {"fields": ("title", "slug", "published")}),
        ("Navigation", {"fields": ("show_in_nav", "nav_label", "nav_order")}),
        ("Content", {"fields": ("body",)}),
    )


@admin.register(FaqItem)
class FaqItemAdmin(admin.ModelAdmin):
    list_display = ("question", "published", "order", "updated_at")
    list_editable = ("published", "order")
    list_filter = ("published",)
    search_fields = ("question", "answer")
