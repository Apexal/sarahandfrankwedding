"""Tests for the wedding-site hardening: rate limiter, staging email check, backups, no-index."""

from urllib.parse import urlsplit

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.checks import staging_has_email_allowlist
from core.ratelimit import is_rate_limited, parse_rate, ratelimit
from core.tasks import GUEST_BACKUP_PREFIX, _backup_guest_data
from guests.models import Household


class TestParseRate:
    @pytest.mark.parametrize(
        ("rate", "expected"),
        [("10/15m", (10, 900)), ("20/h", (20, 3600)), ("5/s", (5, 1)), ("3/2d", (3, 172800))],
    )
    def test_parses(self, rate, expected):
        assert parse_rate(rate) == expected

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_rate("ten per minute")


class TestRateLimit:
    def _request(self, ip="203.0.113.7", method="post"):
        return getattr(RequestFactory(), method)("/x/", REMOTE_ADDR=ip)

    def test_counts_per_ip_within_the_window(self):
        request = self._request()
        assert not is_rate_limited(request, scope="t", count=2, seconds=60)
        assert not is_rate_limited(request, scope="t", count=2, seconds=60)
        assert is_rate_limited(request, scope="t", count=2, seconds=60)
        assert not is_rate_limited(self._request(ip="198.51.100.1"), scope="t", count=2, seconds=60)
        assert not is_rate_limited(request, scope="other", count=2, seconds=60)

    @override_settings(TESTING=False)
    def test_decorator_renders_429_after_the_limit(self):
        @ratelimit("1/m", scope="decorated")
        def view(request):
            return HttpResponse("ok")

        assert view(self._request()).status_code == 200
        response = view(self._request())
        assert response.status_code == 429
        assert b"too many tries" in response.content
        # GETs never count.
        assert view(self._request(method="get")).status_code == 200

    def test_decorator_is_a_noop_under_pytest(self):
        @ratelimit("1/m", scope="noop")
        def view(request):
            return HttpResponse("ok")

        for _ in range(3):
            assert view(self._request()).status_code == 200

    def test_window_resets(self, monkeypatch):
        import core.ratelimit as module

        request = self._request()
        monkeypatch.setattr(module.time, "time", lambda: 1000.0)
        assert not is_rate_limited(request, scope="w", count=1, seconds=60)
        assert is_rate_limited(request, scope="w", count=1, seconds=60)
        monkeypatch.setattr(module.time, "time", lambda: 1061.0)
        cache.clear()
        assert not is_rate_limited(request, scope="w", count=1, seconds=60)


class TestStagingEmailAllowlistCheck:
    def test_staging_without_allowlist_fails(self, monkeypatch):
        monkeypatch.setenv("FLY_APP_NAME", "sarahandfrankwedding-staging")
        with override_settings(EMAIL_RECIPIENT_ALLOWLIST=[]):
            errors = staging_has_email_allowlist(None)
        assert [error.id for error in errors] == ["core.E010"]

    def test_staging_with_allowlist_passes(self, monkeypatch):
        monkeypatch.setenv("FLY_APP_NAME", "sarahandfrankwedding-staging")
        with override_settings(EMAIL_RECIPIENT_ALLOWLIST=["frank@example.com"]):
            assert staging_has_email_allowlist(None) == []

    def test_production_needs_no_allowlist(self, monkeypatch):
        monkeypatch.setenv("FLY_APP_NAME", "sarahandfrankwedding")
        with override_settings(EMAIL_RECIPIENT_ALLOWLIST=[]):
            assert staging_has_email_allowlist(None) == []


@pytest.mark.django_db
class TestGuestBackup:
    def test_writes_a_dump_and_prunes_old_ones(self):
        Household.objects.create(name="The Joneses")
        for day in range(1, 6):
            default_storage.save(f"{GUEST_BACKUP_PREFIX}guests-2026-01-0{day}.json", ContentFile(b"{}"))
        default_storage.save(f"{GUEST_BACKUP_PREFIX}notes.txt", ContentFile(b"keep me"))

        name = _backup_guest_data(keep=3)

        assert name.startswith(f"{GUEST_BACKUP_PREFIX}guests-")
        with default_storage.open(name) as handle:
            dump = handle.read().decode()
        assert '"model": "guests.household"' in dump
        assert "The Joneses" in dump
        _, files = default_storage.listdir(GUEST_BACKUP_PREFIX)
        backups = sorted(f for f in files if f.startswith("guests-"))
        assert len(backups) == 3
        assert backups[-1] == name.removeprefix(GUEST_BACKUP_PREFIX)
        assert "notes.txt" in files

    def test_rerun_on_the_same_day_replaces_the_file(self):
        first = _backup_guest_data()
        Household.objects.create(name="Later")
        second = _backup_guest_data()
        assert first == second
        with default_storage.open(second) as handle:
            assert "Later" in handle.read().decode()


class TestNoIndex:
    def test_robots_disallows_everything(self, client):
        response = client.get("/robots.txt")
        assert response.status_code == 200
        assert "Disallow: /" in response.content.decode().splitlines()

    @pytest.mark.django_db
    def test_pages_carry_a_noindex_meta(self, client):
        html = client.get("/").content.decode()
        assert '<meta name="robots" content="noindex, nofollow">' in html


class TestCsrfOrigins:
    def test_app_base_url_origin_is_trusted(self):
        parts = urlsplit(django_settings.APP_BASE_URL)
        assert f"{parts.scheme}://{parts.netloc}" in django_settings.CSRF_TRUSTED_ORIGINS
