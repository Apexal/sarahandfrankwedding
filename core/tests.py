from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from allauth.mfa.models import Authenticator
from auditlog import get_logentry_model
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.db import connection
from django.http import HttpResponse
from django.template import Context, Template
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core import roles
from core.account_adapter import CoreAccountAdapter, SiteIdentity
from core.admin import UserAdmin
from core.audit import record_object_access
from core.constants import SESSION_IMPERSONATOR_USER_ID_KEY, get_timezone_choices
from core.middleware import TrustedClientIPMiddleware, UserTimezoneMiddleware
from core.models import AppSettings, User
from core.tasks import _clear_expired_sessions, _prune_audit_log
from messaging.models import OutboundEmail


class TestTrustedClientIPMiddleware:
    def _remote_addr_seen_by_view(self, **request_extra):
        seen = {}

        def view(request):
            seen["remote_addr"] = request.META.get("REMOTE_ADDR")
            return HttpResponse()

        middleware = TrustedClientIPMiddleware(view)
        middleware(RequestFactory().get("/", **request_extra))
        return seen["remote_addr"]

    @override_settings(TRUSTED_CLIENT_IP_HEADER="Fly-Client-IP")
    def test_rewrites_remote_addr_from_trusted_header(self):
        assert self._remote_addr_seen_by_view(HTTP_FLY_CLIENT_IP="203.0.113.7") == "203.0.113.7"

    @override_settings(TRUSTED_CLIENT_IP_HEADER="Fly-Client-IP")
    def test_keeps_remote_addr_when_header_absent(self):
        assert self._remote_addr_seen_by_view() == "127.0.0.1"

    @override_settings(TRUSTED_CLIENT_IP_HEADER=None)
    def test_noop_when_not_configured(self):
        assert self._remote_addr_seen_by_view(HTTP_FLY_CLIENT_IP="203.0.113.7") == "127.0.0.1"


@pytest.mark.django_db
class TestAnonymousAccess:
    def test_home_page_loads(self, client):
        assert client.get("/").status_code == 200

    def test_profile_requires_login(self, client):
        response = client.get("/profile/")
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_robots_txt(self, client):
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert response["Content-Type"] == "text/plain"
        assert "Disallow: /admin/" in response.content.decode()

    def test_favicon_returns_204(self, client):
        assert client.get("/favicon.ico").status_code == 204


@pytest.mark.django_db
class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        response = client.get("/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}

    def test_db_error_is_not_echoed_to_caller(self, client, monkeypatch, posthog_exceptions):
        # Prime the AppSettings cache first: MaintenanceModeMiddleware would
        # otherwise query the DB (through ensure_connection) on this request.
        AppSettings.load()
        monkeypatch.setattr(
            connection, "ensure_connection", lambda: (_ for _ in ()).throw(RuntimeError("secret /data/db path"))
        )

        response = client.get("/health/")

        assert response.status_code == 503
        assert response.json() == {"status": "error", "database": "unavailable"}
        assert "secret" not in response.content.decode()
        [(_, kwargs)] = posthog_exceptions
        assert kwargs["properties"]["handler"] == "health"


@pytest.mark.django_db
class TestMaintenanceMode:
    @pytest.fixture(autouse=True)
    def _maintenance_on(self, db):
        app_settings = AppSettings.load()
        app_settings.maintenance_mode = True
        app_settings.save()

    def test_non_staff_sees_503(self, client):
        assert client.get("/").status_code == 503

    def test_health_is_whitelisted(self, client):
        assert client.get("/health/").status_code == 200

    def test_staff_passes_through(self, admin_client):
        assert admin_client.get("/").status_code == 200


class TestTimezoneChoices:
    def test_all_choices_are_valid_iana_zones(self):
        for _group, choices in get_timezone_choices():
            for tz, _label in choices:
                ZoneInfo(tz)  # raises if not a real zone


@pytest.mark.django_db
class TestUserTimezoneMiddleware:
    def _active_tz_seen_by_view(self, request_user):
        seen = {}

        def view(request):
            seen["tz"] = dj_timezone.get_current_timezone_name()
            return HttpResponse()

        request = RequestFactory().get("/")
        request.user = request_user
        UserTimezoneMiddleware(view)(request)
        return seen["tz"]

    def test_activates_user_timezone(self, user):
        user.time_zone = "America/Los_Angeles"
        user.save()
        assert self._active_tz_seen_by_view(user) == "America/Los_Angeles"

    def test_falls_back_for_anonymous(self):
        with override_settings(TIME_ZONE="America/New_York"):
            assert self._active_tz_seen_by_view(AnonymousUser()) == "America/New_York"

    def test_the_fallback_tracks_the_project_timezone(self):
        with override_settings(TIME_ZONE="Asia/Tokyo"):
            assert self._active_tz_seen_by_view(AnonymousUser()) == "Asia/Tokyo"


@pytest.mark.django_db
class TestRequireStaffMfa:
    @pytest.fixture(autouse=True)
    def _require_mfa(self, settings):
        settings.MFA_REQUIRED_FOR_STAFF = True

    def test_staff_without_mfa_is_redirected_to_setup(self, admin_client):
        response = admin_client.get("/admin/")
        assert response.status_code == 302
        assert response.url == reverse("mfa_index")

    def test_staff_with_totp_is_not_redirected(self, admin_client, admin_user):
        Authenticator.objects.create(user=admin_user, type=Authenticator.Type.TOTP, data={})
        assert admin_client.get("/admin/").status_code == 200

    def test_recovery_codes_alone_do_not_satisfy_requirement(self, admin_client, admin_user):
        Authenticator.objects.create(user=admin_user, type=Authenticator.Type.RECOVERY_CODES, data={})
        response = admin_client.get("/admin/")
        assert response.status_code == 302
        assert response.url == reverse("mfa_index")

    def test_non_staff_is_not_redirected(self, user_client):
        assert user_client.get("/profile/").status_code == 200

    def test_mfa_setup_pages_are_exempt(self, admin_client):
        assert admin_client.get(reverse("mfa_index")).status_code == 200

    def test_disabled_via_setting(self, admin_client, settings):
        settings.MFA_REQUIRED_FOR_STAFF = False
        assert admin_client.get("/admin/").status_code == 200


@pytest.mark.django_db
class TestUserAdminHardening:
    """Staff with change_user permission must not be able to escalate privileges."""

    ESCALATION_FIELDS = {"is_staff", "is_superuser", "groups", "user_permissions"}

    @pytest.fixture
    def staff_with_change_perm(self, staff_user):
        staff_user.user_permissions.add(Permission.objects.get(codename="change_user"))
        # Re-fetch so the permission cache is fresh
        return User.objects.get(pk=staff_user.pk)

    def _request_as(self, requesting_user):
        request = RequestFactory().get("/")
        request.user = requesting_user
        return request

    def test_permission_fields_readonly_for_staff(self, staff_with_change_perm, user):
        user_admin = UserAdmin(User, admin.site)
        readonly = user_admin.get_readonly_fields(self._request_as(staff_with_change_perm), user)
        assert set(readonly) >= self.ESCALATION_FIELDS

    def test_permission_fields_editable_for_superuser(self, admin_user, user):
        user_admin = UserAdmin(User, admin.site)
        readonly = user_admin.get_readonly_fields(self._request_as(admin_user), user)
        assert not (self.ESCALATION_FIELDS & set(readonly))

    def test_staff_cannot_change_or_delete_superusers(self, staff_with_change_perm, admin_user, user):
        user_admin = UserAdmin(User, admin.site)
        request = self._request_as(staff_with_change_perm)
        assert user_admin.has_change_permission(request, user)  # regular users: allowed
        assert not user_admin.has_change_permission(request, admin_user)
        assert not user_admin.has_delete_permission(request, admin_user)


@pytest.mark.django_db
class TestImpersonation:
    def _impersonate(self, admin_client, target):
        return admin_client.post(reverse("admin:core_user_impersonate", args=[target.pk]))

    def test_superuser_can_impersonate_regular_user(self, admin_client, admin_user, user):
        response = self._impersonate(admin_client, user)
        assert response.status_code == 302
        assert admin_client.session[SESSION_IMPERSONATOR_USER_ID_KEY] == admin_user.pk

    def test_cannot_impersonate_inactive_user(self, admin_client, user):
        user.is_active = False
        user.save()
        self._impersonate(admin_client, user)
        assert SESSION_IMPERSONATOR_USER_ID_KEY not in admin_client.session

    def test_cannot_impersonate_staff(self, admin_client, staff_user):
        self._impersonate(admin_client, staff_user)
        assert SESSION_IMPERSONATOR_USER_ID_KEY not in admin_client.session


@pytest.mark.django_db
class TestImpersonationAuditTrail:
    """Audited changes made while impersonating are attributed to the impersonator.

    Exercises the full middleware stack: ImpersonationAuditMiddleware must run
    after AuditlogMiddleware so its actor override wins.
    """

    def test_audit_actor_is_rewritten_to_impersonator(self, admin_client, admin_user, user):
        admin_client.post(reverse("admin:core_user_impersonate", args=[user.pk]))
        admin_client.post("/profile/", {"user-first_name": "Changed"})

        user.refresh_from_db()
        assert user.first_name == "Changed"
        log_entry_model = get_logentry_model()
        entry = log_entry_model.objects.get(object_pk=str(user.pk), action=log_entry_model.Action.UPDATE)
        assert entry.actor == admin_user
        assert entry.additional_data["impersonation"] is True
        assert entry.additional_data["impersonated_user_id"] == user.pk


@pytest.mark.django_db
class TestStopImpersonation:
    def _start_impersonating(self, admin_client, target):
        admin_client.post(reverse("admin:core_user_impersonate", args=[target.pk]))

    def test_restores_original_superuser_session(self, admin_client, admin_user, user):
        self._start_impersonating(admin_client, user)

        response = admin_client.post(reverse("core:stop_impersonation"))

        assert response.status_code == 302
        assert response.url == reverse("admin:index")
        assert admin_client.session["_auth_user_id"] == str(admin_user.pk)
        assert SESSION_IMPERSONATOR_USER_ID_KEY not in admin_client.session

    def test_noop_when_not_impersonating(self, user_client, user):
        response = user_client.post(reverse("core:stop_impersonation"))

        assert response.status_code == 302
        assert response.url == reverse("core:home")
        assert user_client.session["_auth_user_id"] == str(user.pk)  # still themselves

    def test_demoted_impersonator_is_not_restored(self, admin_client, admin_user, user):
        self._start_impersonating(admin_client, user)
        admin_user.is_superuser = False
        admin_user.save()

        admin_client.post(reverse("core:stop_impersonation"))

        # Session stays as the impersonated user rather than restoring a non-superuser.
        assert admin_client.session["_auth_user_id"] == str(user.pk)

    def test_requires_post(self, user_client):
        assert user_client.get(reverse("core:stop_impersonation")).status_code == 405


@pytest.mark.django_db
class TestHousekeepingTasks:
    def test_prune_audit_log_deletes_only_old_entries(self, user, admin_user):
        old_entry = record_object_access(accessed_object=user, actor=admin_user)
        recent_entry = record_object_access(accessed_object=admin_user, actor=admin_user)
        log_entry_model = get_logentry_model()
        log_entry_model.objects.filter(pk=old_entry.pk).update(timestamp=dj_timezone.now() - timedelta(days=400))

        _prune_audit_log()

        assert not log_entry_model.objects.filter(pk=old_entry.pk).exists()
        assert log_entry_model.objects.filter(pk=recent_entry.pk).exists()

    def test_clear_expired_sessions(self, db):
        Session.objects.create(
            session_key="expired", session_data="x", expire_date=dj_timezone.now() - timedelta(days=1)
        )
        Session.objects.create(
            session_key="active", session_data="x", expire_date=dj_timezone.now() + timedelta(days=1)
        )

        _clear_expired_sessions()

        assert not Session.objects.filter(session_key="expired").exists()
        assert Session.objects.filter(session_key="active").exists()


@pytest.mark.django_db
class TestAllauthEmailContext:
    """allauth's stock templates open "Hello from {{ current_site.name }}!".

    `CoreAccountAdapter.send_mail` overrides the one method that supplies
    `current_site`, so without seeding it back the blank goes out in every
    password reset, verification and security-notification email — and nothing
    fails, which is how it shipped.
    """

    def _send(self, template_prefix="account/email/password_reset_key", context=None):
        CoreAccountAdapter().send_mail(
            template_prefix,
            "someone@example.com",
            {"password_reset_url": "https://example.test/reset", **(context or {})},
        )
        return OutboundEmail.objects.latest("pk")

    def test_the_site_name_is_rendered_not_a_blank(self):
        app_settings = AppSettings.load()
        app_settings.site_name = "Acme Health"
        app_settings.save()

        email = self._send()

        assert "Hello from Acme Health!" in email.body_text_rendered
        assert "Thank you for using Acme Health!" in email.body_text_rendered

    @override_settings(APP_BASE_URL="https://app.example.com")
    def test_the_domain_comes_from_app_base_url_without_the_scheme(self):
        email = self._send()

        assert "app.example.com" in email.body_text_rendered
        assert "https://app.example.com\n" not in email.body_text_rendered

    def test_it_renders_with_no_request_in_scope(self):
        """A management command or a queue worker has no request, and
        `get_current_site(None)` — what allauth would have called — raises."""
        from allauth.core import context as allauth_context

        assert allauth_context.request is None

        assert AppSettings.load().site_name in self._send().body_text_rendered

    def test_the_subject_is_not_prefixed_with_the_deployment_host(self):
        """Unset, ACCOUNT_EMAIL_SUBJECT_PREFIX makes allauth build the prefix
        from the request's host, leaking it into the inbox."""
        assert self._send().subject_rendered == "Password Reset Email"

    def test_caller_context_still_wins(self):
        """The seeded keys are defaults, mirroring allauth's ctx.update(context)."""
        email = self._send(context={"current_site": SiteIdentity()})

        assert AppSettings.load().site_name in email.body_text_rendered


@pytest.mark.django_db
class TestEmailLogin:
    LOGIN_URL = "/accounts/login/"

    def test_login_with_email(self, client, user):
        response = client.post(self.LOGIN_URL, {"login": user.email, "password": user.raw_password})
        assert response.status_code == 302
        assert response.url == "/"

    def test_unverified_email_cannot_log_in(self, client):
        User.objects.create_user(username="unverified", email="unverified@example.com", password="testpass123")
        response = client.post(self.LOGIN_URL, {"login": "unverified@example.com", "password": "testpass123"})
        assert response.status_code == 302
        assert "_auth_user_id" not in client.session  # blocked pending verification

    def test_signup_closed_by_default(self, client):
        response = client.get("/accounts/signup/")
        assert "account/signup_closed.html" in [t.name for t in response.templates]


@pytest.mark.django_db
class TestCsrfFailureView:
    """CSRF_FAILURE_VIEW serves a friendly 403 and reports the reason to PostHog."""

    URL = "/profile/"  # any non-exempt POST route; CSRF runs before the view's login_required

    def _post_without_token(self):
        return Client(enforce_csrf_checks=True).post(self.URL, {"user-first_name": "Nope"})

    def test_renders_custom_page_with_403(self):
        response = self._post_without_token()
        assert response.status_code == 403
        assert "core/csrf_failure.html" in [t.name for t in response.templates]
        assert "Your session expired" in response.content.decode()

    def test_reports_reason_to_posthog(self, posthog_exceptions):
        self._post_without_token()

        [(_, kwargs)] = posthog_exceptions
        assert kwargs["distinct_id"] == "csrf_failure"
        properties = kwargs["properties"]
        assert properties["handler"] == "csrf_failure"
        assert properties["error_type"] == "CsrfFailure"
        assert properties["path"] == self.URL
        assert properties["method"] == "POST"
        assert properties["reason"]  # Django always supplies a cause


@pytest.mark.django_db
class TestSyncGroups:
    """`sync_groups` makes the database match core.roles.GROUP_PERMISSIONS exactly."""

    @staticmethod
    def _group_perms(name):
        group = Group.objects.get(name=name)
        return sorted(f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all())

    def test_creates_declared_groups_with_exact_permissions(self, monkeypatch):
        monkeypatch.setattr(roles, "GROUP_PERMISSIONS", {"Support": ["core.view_user", "core.change_user"]})

        call_command("sync_groups")

        assert self._group_perms("Support") == ["core.change_user", "core.view_user"]

    def test_resync_removes_permissions_no_longer_declared(self, monkeypatch):
        monkeypatch.setattr(roles, "GROUP_PERMISSIONS", {"Support": ["core.view_user", "core.change_user"]})
        call_command("sync_groups")

        monkeypatch.setattr(roles, "GROUP_PERMISSIONS", {"Support": ["core.view_user"]})
        call_command("sync_groups")

        assert self._group_perms("Support") == ["core.view_user"]

    def test_unknown_permission_fails_loudly(self, monkeypatch):
        monkeypatch.setattr(roles, "GROUP_PERMISSIONS", {"Support": ["core.does_not_exist"]})

        with pytest.raises(CommandError, match="core.does_not_exist"):
            call_command("sync_groups")

    def test_undeclared_group_is_kept_unless_pruned(self, monkeypatch):
        monkeypatch.setattr(roles, "GROUP_PERMISSIONS", {})
        Group.objects.create(name="Legacy")

        call_command("sync_groups")
        assert Group.objects.filter(name="Legacy").exists()

        call_command("sync_groups", "--prune")
        assert not Group.objects.filter(name="Legacy").exists()


class TestCkeditorMedia:
    """{% ckeditor_media %} must emit bundle.js at most once per request.

    A second execution of the bundle resets window.editors and
    ckeditorRegisterCallback, orphaning JS hooks attached to the first one.
    """

    BUNDLE = "django_ckeditor_5/dist/bundle.js"
    TEMPLATE = Template("{% load core_filters %}{% ckeditor_media %}{% ckeditor_media %}")

    def test_duplicate_calls_render_bundle_once(self, rf):
        html = self.TEMPLATE.render(Context({"request": rf.get("/")}))
        assert html.count(self.BUNDLE) == 1

    def test_each_request_gets_its_own_include(self, rf):
        assert self.TEMPLATE.render(Context({"request": rf.get("/")})).count(self.BUNDLE) == 1
        assert self.TEMPLATE.render(Context({"request": rf.get("/")})).count(self.BUNDLE) == 1

    def test_renders_without_a_request_in_context(self):
        assert self.TEMPLATE.render(Context()).count(self.BUNDLE) >= 1

    def test_upload_is_staff_only(self, client, user_client):
        # CKEDITOR_5_FILE_UPLOAD_PERMISSION = "staff"
        url = reverse("ck_editor_5_upload_file")
        assert client.post(url).status_code in (302, 403)
        assert user_client.post(url).status_code in (302, 403)
