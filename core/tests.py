from datetime import timedelta

import pytest
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.contrib.sessions.models import Session
from django.db import connection
from django.http import HttpResponse
from django.template import Context, Template
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone as dj_timezone

from core.account_adapter import CoreAccountAdapter, SiteIdentity
from core.admin import UserAdmin
from core.middleware import TrustedClientIPMiddleware
from core.models import User
from core.tasks import _clear_expired_sessions
from messaging.models import OutboundEmail
from pages.models import WeddingSettings


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

    def test_robots_txt(self, client):
        response = client.get("/robots.txt")
        assert response.status_code == 200
        # A private site: everything is disallowed (see core/test_hygiene.py for the meta tag).
        assert "Disallow: /" in response.content.decode().splitlines()

    def test_favicon_returns_204(self, client):
        assert client.get("/favicon.ico").status_code == 204


@pytest.mark.django_db
class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        response = client.get("/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "connected"}

    def test_db_error_is_not_echoed_to_caller(self, client, monkeypatch):
        monkeypatch.setattr(
            connection, "ensure_connection", lambda: (_ for _ in ()).throw(RuntimeError("secret /data/db path"))
        )

        response = client.get("/health/")

        assert response.status_code == 503
        assert response.json() == {"status": "error", "database": "unavailable"}
        assert "secret" not in response.content.decode()


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
        assert user_client.get("/").status_code == 200

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
class TestHousekeepingTasks:
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

    def test_the_couple_names_are_rendered_not_a_blank(self):
        wedding = WeddingSettings.load()
        wedding.couple_names = "Sarah & Frank"
        wedding.save()

        email = self._send()

        assert "Hello from Sarah & Frank!" in email.body_text_rendered
        assert "Thank you for using Sarah & Frank!" in email.body_text_rendered

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

        assert WeddingSettings.load().couple_names in self._send().body_text_rendered

    def test_the_subject_is_not_prefixed_with_the_deployment_host(self):
        """Unset, ACCOUNT_EMAIL_SUBJECT_PREFIX makes allauth build the prefix
        from the request's host, leaking it into the inbox."""
        assert self._send().subject_rendered == "Password Reset Email"

    def test_caller_context_still_wins(self):
        """The seeded keys are defaults, mirroring allauth's ctx.update(context)."""
        email = self._send(context={"current_site": SiteIdentity()})

        assert WeddingSettings.load().couple_names in email.body_text_rendered


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

    def test_signup_is_always_closed(self, client):
        response = client.get("/accounts/signup/")
        assert "account/signup_closed.html" in [t.name for t in response.templates]


@pytest.mark.django_db
class TestCsrfFailureView:
    """CSRF_FAILURE_VIEW serves a friendly 403 and logs the reason."""

    URL = "/"  # any non-exempt route: CsrfViewMiddleware rejects the POST before the view runs

    def _post_without_token(self):
        return Client(enforce_csrf_checks=True).post(self.URL, {"anything": "Nope"})

    def test_renders_custom_page_with_403(self):
        response = self._post_without_token()
        assert response.status_code == 403
        assert "core/csrf_failure.html" in [t.name for t in response.templates]
        assert "Your session expired" in response.content.decode()


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
