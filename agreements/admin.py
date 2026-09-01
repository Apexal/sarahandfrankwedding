from auditlog.mixins import AuditlogHistoryAdminMixin
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from agreements.models import Agreement, AgreementVersion, UserAgreement


class AgreementVersionInline(admin.TabularInline):
    model = AgreementVersion
    fk_name = "agreement"
    extra = 0
    show_change_link = True
    fields = ("version", "effective_date", "published_by", "published_at", "created_at")
    readonly_fields = ("version", "effective_date", "published_by", "published_at", "created_at")
    ordering = ("-version",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # Versions are added from the AgreementVersion admin, where the full text is editable.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Agreement)
class AgreementAdmin(AuditlogHistoryAdminMixin, admin.ModelAdmin):
    show_auditlog_history_link = True
    list_display = ("name", "key", "required", "requires_reacceptance", "current_version", "created_at")
    list_filter = ("required", "requires_reacceptance")
    list_select_related = ("current_version",)
    search_fields = ("name", "key")
    prepopulated_fields = {"key": ("name",)}
    readonly_fields = ("current_version", "public_url", "created_at", "updated_at")
    inlines = (AgreementVersionInline,)

    @admin.display(description="Public page")
    def public_url(self, obj):
        if not obj.pk:
            return "—"
        url = obj.get_absolute_url()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    def history_view(self, request, object_id, extra_context=None):
        return redirect(reverse("admin:agreements_agreement_auditlog", args=[object_id]))


@admin.register(AgreementVersion)
class AgreementVersionAdmin(AuditlogHistoryAdminMixin, admin.ModelAdmin):
    show_auditlog_history_link = True
    list_display = ("__str__", "agreement", "version", "effective_date", "published_by", "is_current", "created_at")
    list_select_related = ("agreement", "published_by", "agreement__current_version")
    list_filter = ("agreement",)
    search_fields = ("agreement__name", "agreement__key")
    actions = ("publish_version",)

    @admin.display(boolean=True, description="Current")
    def is_current(self, obj):
        return obj.is_current

    @admin.display(description="Acceptances")
    def acceptances(self, obj):
        """Count plus a link, rather than an inline.

        A widely-accepted version has as many UserAgreement rows as it has users, and
        an unbounded inline would build a form per row and make this page — the one
        staff open to publish — impossible to load.
        """
        if not obj.pk:
            return "—"
        count = obj.user_agreements.count()
        url = reverse("admin:agreements_useragreement_changelist")
        return format_html('<a href="{}?agreement_version__id__exact={}">{} acceptance(s)</a>', url, obj.pk, count)

    def has_delete_permission(self, request, obj=None):
        """Published versions are part of the consent trail and cannot be deleted.

        Deleting the version an Agreement points at would SET_NULL `current_version`
        through a bulk update — no auditlog entry, and the gate silently stops asking
        anyone to accept anything. Unpublished drafts are still disposable; bulk
        deletion is refused outright since it cannot be judged per object here.
        """
        if obj is None:
            return False
        if obj.published_at or obj.is_current or obj.user_agreements.exists():
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Publish selected version (makes it the current one for its agreement)")
    def publish_version(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one version to publish.", level=messages.ERROR)
            return
        version = queryset.select_related("agreement").first()
        version.publish(published_by=request.user)
        self.message_user(
            request,
            f"Published {version} as the current version of '{version.agreement.name}'. "
            "Users will be asked to accept it on their next request.",
        )

    def get_readonly_fields(self, request, obj=None):
        if obj:
            # Published text is immutable — a change of terms means a new version,
            # so that every UserAgreement keeps pointing at the exact text accepted.
            return (
                "agreement",
                "version",
                "content_markdown",
                "effective_date",
                "published_by",
                "published_at",
                "acceptances",
                "created_at",
                "updated_at",
            )
        # `version` is omitted entirely on the add form: the model assigns the next
        # number for the chosen agreement.
        return ("published_by", "published_at", "created_at", "updated_at")

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj is None:
            fields = [field for field in fields if field != "version"]
        return fields

    def history_view(self, request, object_id, extra_context=None):
        return redirect(reverse("admin:agreements_agreementversion_auditlog", args=[object_id]))


@admin.register(UserAgreement)
class UserAgreementAdmin(admin.ModelAdmin):
    """Read-only consent trail. Records are never added, modified, or deleted here."""

    list_display = ("user", "agreement_version", "created_at", "ip_address")
    list_select_related = ("user", "agreement_version__agreement")
    # `agreement_version` is filterable so the AgreementVersion change page can link
    # here scoped to one version — the paginated replacement for an inline roster.
    list_filter = ("agreement_version__agreement", "agreement_version")
    search_fields = ("user__email", "user__username", "user__first_name", "user__last_name")
    readonly_fields = ("user", "agreement_version", "created_at", "updated_at", "ip_address", "user_agent")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
