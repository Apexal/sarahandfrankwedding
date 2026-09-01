import posthog
from allauth.account.decorators import secure_admin_login
from auditlog import get_logentry_model
from auditlog.filters import CIDFilter, ResourceTypeFilter
from auditlog.mixins import AuditlogHistoryAdminMixin, LogEntryAdminMixin
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import BACKEND_SESSION_KEY, get_user_model, login
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from core.constants import COMPANY, SESSION_IMPERSONATOR_USER_ID_KEY
from core.models import AccessToken, AccountDeletionRequest, AppSettings, RefreshToken, User

admin.autodiscover()
admin.site.login = secure_admin_login(admin.site.login)

admin.site.site_header = f"{COMPANY['name']} Portal Admin"
admin.site.site_title = f"{COMPANY['name']} Portal Admin"

LogEntry = get_logentry_model()
admin.site.unregister(LogEntry)
admin.site.unregister(Group)


@admin.register(LogEntry)
class CustomLogEntryAdmin(admin.ModelAdmin, LogEntryAdminMixin):
    date_hierarchy = "timestamp"
    list_select_related = ["content_type", "actor"]
    list_display = [
        "created",
        "resource_url",
        "action",
        "msg_short",
        "user_url",
        "impersonation_display",
        "cid_url",
    ]
    search_fields = [
        "timestamp",
        "object_repr",
        "changes",
        "actor__first_name",
        "actor__last_name",
        f"actor__{get_user_model().USERNAME_FIELD}",
    ]
    list_filter = ["action", ResourceTypeFilter, CIDFilter]
    readonly_fields = ["created", "resource_url", "action", "user_url", "msg", "impersonation_display"]
    fieldsets = [
        (None, {"fields": ["created", "user_url", "resource_url", "cid"]}),
        (_("Changes"), {"fields": ["action", "msg"]}),
        (_("Impersonation"), {"fields": ["impersonation_display"]}),
    ]

    @admin.display(description="Impersonation")
    def impersonation_display(self, obj):
        data = obj.additional_data or {}
        if not data.get("impersonation"):
            return "-"
        username = data.get("impersonated_username", "unknown")
        user_id = data.get("impersonated_user_id")
        label = f"Impersonating {username}"
        if user_id:
            url = reverse("admin:core_user_change", args=[user_id])
            label = format_html(
                '<span style="color:#991b1b;font-weight:600;">⚠ Impersonating </span><a href="{}">{}</a>',
                url,
                username,
            )
        return label

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        self.request = request
        return super().get_queryset(request=request)


@admin.register(AppSettings)
class AppSettingsAdmin(AuditlogHistoryAdminMixin, admin.ModelAdmin):
    show_auditlog_history_link = True

    def has_add_permission(self, request):
        return not AppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AppSettings.load()
        return redirect("admin:core_appsettings_change", obj.pk)

    def history_view(self, request, object_id, extra_context=None):
        return redirect(reverse("admin:core_appsettings_auditlog", args=[object_id]))


@admin.register(User)
class UserAdmin(AuditlogHistoryAdminMixin, BaseUserAdmin):
    change_form_template = "admin/core/user/change_form.html"
    show_auditlog_history_link = True

    def history_view(self, request, object_id, extra_context=None):
        return redirect(reverse("admin:core_user_auditlog", args=[object_id]))

    list_filter = ("groups",)
    search_fields = ("email", "first_name", "last_name")
    list_display = ("name", "email", "is_active")

    fieldsets = (
        (None, {"fields": ("username", "password")}),
        (
            "Personal info",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "phone_number",
                    "time_zone",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "password1",
                    "password2",
                ),
            },
        ),
        (
            "Personal info",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                    "phone_number",
                    "time_zone",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        # Staff with change_user permission must not be able to escalate
        # themselves (or anyone) to staff/superuser or grant permissions.
        readonly = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            readonly = (*readonly, "is_staff", "is_superuser", "groups", "user_permissions")
        return readonly

    def has_change_permission(self, request, obj=None):
        # Only superusers may edit superuser accounts — this also guards the
        # admin password-change view, which checks change permission.
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:user_id>/impersonate/",
                self.admin_site.admin_view(self.impersonate_user_view),
                name="core_user_impersonate",
            ),
        ]
        return custom_urls + urls

    def impersonate_user_view(self, request, user_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not request.user.is_superuser:
            raise PermissionDenied

        target_user = get_object_or_404(User, pk=user_id)
        if target_user.pk == request.user.pk:
            messages.warning(request, "You are already logged in as this user.")
            return redirect("admin:core_user_change", target_user.pk)
        if target_user.is_staff or target_user.is_superuser:
            messages.error(request, "You cannot impersonate staff or superuser accounts.")
            return redirect("admin:core_user_change", target_user.pk)
        if not target_user.is_active:
            # login() would succeed but ModelBackend rejects inactive users on
            # the next request, stranding the impersonator in a logged-out session.
            messages.error(request, "You cannot impersonate an inactive account.")
            return redirect("admin:core_user_change", target_user.pk)

        impersonator_user_id = request.user.pk
        posthog.capture(
            "user_impersonated",
            distinct_id=str(impersonator_user_id),
            properties={"target_user_id": str(target_user.pk), "target_email": target_user.email},
        )
        backend = request.session.get(BACKEND_SESSION_KEY, settings.AUTHENTICATION_BACKENDS[0])
        login(request, target_user, backend=backend)
        request.session[SESSION_IMPERSONATOR_USER_ID_KEY] = impersonator_user_id
        messages.success(request, f"You are now impersonating {target_user}.")
        return redirect("core:home")


@admin.register(AccountDeletionRequest)
class AccountDeletionRequestAdmin(AuditlogHistoryAdminMixin, admin.ModelAdmin):
    """Support view for in-app deletion requests.

    Read-only on purpose: the lifecycle belongs to the API and the nightly sweep.
    Rows with no user are completed deletions — the account is gone, the record
    that it happened is not.
    """

    list_display = ("id", "user", "status", "scheduled_for", "completed_at", "created_at")
    list_filter = ("status",)
    search_fields = ("user__email",)
    date_hierarchy = "scheduled_for"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class HashedTokenAdmin(admin.ModelAdmin):
    """Base admin for API tokens. The raw token is never stored, so there is nothing to show."""

    list_display = ("id", "user", "name", "created_at", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("user__email", "name")
    actions = ("revoke_selected",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="Revoke selected tokens")
    def revoke_selected(self, request, queryset):
        revoked = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now(), updated_at=timezone.now())
        self.message_user(request, f"Revoked {revoked} token(s).", messages.SUCCESS)


@admin.register(AccessToken)
class AccessTokenAdmin(HashedTokenAdmin):
    pass


@admin.register(RefreshToken)
class RefreshTokenAdmin(HashedTokenAdmin):
    pass
