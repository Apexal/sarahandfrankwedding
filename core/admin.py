from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group

from core.constants import COMPANY
from core.models import User

admin.autodiscover()
admin.site.login = secure_admin_login(admin.site.login)

admin.site.site_header = f"{COMPANY['name']} Portal Admin"
admin.site.site_title = f"{COMPANY['name']} Portal Admin"

admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
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
