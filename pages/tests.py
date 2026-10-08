from datetime import date

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.urls import reverse

from pages.models import FaqItem, Page, WeddingSettings, validate_registry_links
from pages.views import date_span


@pytest.mark.django_db
class TestWeddingSettings:
    def test_load_creates_the_singleton_with_defaults(self):
        settings = WeddingSettings.load()
        assert settings.pk == 1
        assert settings.couple_names == "Sarah and Frank"
        assert settings.wedding_date == date(2027, 9, 18)
        assert settings.gate_enabled is False

    def test_save_forces_pk_one_and_busts_the_cache(self):
        first = WeddingSettings.load()
        assert cache.get(WeddingSettings.CACHE_KEY) is not None
        second = WeddingSettings(couple_names="S & F")
        second.save()
        assert second.pk == 1
        assert cache.get(WeddingSettings.CACHE_KEY) is None
        assert WeddingSettings.objects.count() == 1
        assert WeddingSettings.load().couple_names == "S & F"
        assert first.pk == second.pk

    def test_password_is_compared_stripped_and_casefolded(self):
        settings = WeddingSettings.load()
        settings.site_password = "Willow2027"
        settings.save()
        assert settings.check_site_password("  willow2027 ")
        assert settings.check_site_password("WILLOW2027")
        assert not settings.check_site_password("willow")

    def test_blank_password_means_open_gate(self):
        settings = WeddingSettings.load()
        assert settings.site_password_fingerprint == ""
        assert settings.check_site_password("anything")

    def test_fingerprint_changes_with_the_password(self):
        settings = WeddingSettings(site_password="willow")
        first = settings.site_password_fingerprint
        assert len(first) == 16
        assert WeddingSettings(site_password="Willow").site_password_fingerprint == first
        assert WeddingSettings(site_password="oak").site_password_fingerprint != first

    @pytest.mark.parametrize(
        ("rsvp_open", "deadline", "event_deadline", "today", "expected"),
        [
            (False, date(2027, 8, 14), None, date(2027, 1, 1), False),
            (True, None, None, date(2027, 12, 31), True),
            (True, date(2027, 8, 14), None, date(2027, 8, 14), True),
            (True, date(2027, 8, 14), None, date(2027, 8, 15), False),
            (True, date(2027, 8, 14), date(2027, 7, 1), date(2027, 7, 2), False),
            (True, date(2027, 7, 1), date(2027, 8, 14), date(2027, 7, 2), True),
        ],
    )
    def test_rsvp_is_open(self, rsvp_open, deadline, event_deadline, today, expected):
        settings = WeddingSettings(rsvp_open=rsvp_open, rsvp_deadline=deadline)

        class FakeEvent:
            rsvp_deadline = event_deadline

        assert settings.rsvp_is_open(FakeEvent(), today=today) is expected

    def test_registry_links_validator(self):
        validate_registry_links([])
        validate_registry_links([{"label": "Zola", "url": "https://zola.com/x"}])
        for bad in (
            "not a list",
            [{"label": "x"}],
            [{"label": "", "url": "https://a.b"}],
            [{"label": "x", "url": "not a url"}],
            [{"label": "x", "url": "https://a.b", "extra": 1}],
        ):
            with pytest.raises(ValidationError):
                validate_registry_links(bad)


@pytest.mark.parametrize(
    ("names", "expected"),
    [("Sarah & Frank", "S & F"), ("Sarah and Frank", "S & F"), ("sarah&frank", "S & F"), ("Sarah", "S")],
)
def test_monogram(names, expected):
    assert WeddingSettings(couple_names=names).monogram == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        ([], ""),
        ([date(2027, 9, 18), date(2027, 9, 18)], "September 18, 2027"),
        ([date(2027, 9, 18), date(2027, 9, 17)], "September 17 & 18, 2027"),
        ([date(2027, 9, 17), date(2027, 9, 18), date(2027, 9, 19)], "September 17 – 19, 2027"),
        ([date(2027, 9, 30), date(2027, 10, 1)], "September 30 & October 1, 2027"),
        ([date(2027, 12, 31), date(2028, 1, 1)], "December 31, 2027 & January 1, 2028"),
    ],
)
def test_date_span(days, expected):
    assert date_span(days) == expected


@pytest.mark.django_db
class TestPage:
    def test_reserved_slug_is_rejected(self):
        page = Page(slug="rsvp", title="RSVP")
        with pytest.raises(ValidationError, match="fixed route"):
            page.full_clean()

    def test_ordinary_slug_is_accepted(self):
        Page(slug="our-story", title="Our Story").full_clean()

    def test_nav_text_falls_back_to_title(self):
        assert Page(title="Our Story").nav_text == "Our Story"
        assert Page(title="Our Story", nav_label="Story").nav_text == "Story"

    def test_faq_orders_by_order_then_id(self):
        second = FaqItem.objects.create(question="B", order=2)
        first = FaqItem.objects.create(question="A", order=1)
        assert list(FaqItem.objects.all()) == [first, second]


@pytest.mark.django_db
class TestAdmin:
    def test_wedding_settings_changelist_redirects_to_the_singleton(self, admin_client):
        response = admin_client.get(reverse("admin:pages_weddingsettings_changelist"))
        assert response.status_code == 302
        assert response.url == reverse("admin:pages_weddingsettings_change", args=[1])
        assert admin_client.get(response.url).status_code == 200

    def test_wedding_settings_cannot_be_added_twice(self, admin_client):
        WeddingSettings.load()
        response = admin_client.get(reverse("admin:pages_weddingsettings_add"))
        assert response.status_code == 403

    def test_wedding_settings_can_be_saved_from_the_admin(self, admin_client):
        settings = WeddingSettings.load()
        response = admin_client.post(
            reverse("admin:pages_weddingsettings_change", args=[settings.pk]),
            {
                "couple_names": "Sarah & Frank",
                "wedding_date": "2027-09-18",
                "wedding_city": "Chicago, Illinois",
                "contact_email": "hello@example.com",
                "site_password": "",
                "save_the_date_message": "",
                "rsvp_intro": "",
                "rsvp_closed_message": "",
                "photo_album_url": "",
                "photo_album_note": "",
                "registry_links": "[]",
            },
        )
        assert response.status_code == 302, response.content
        assert WeddingSettings.load().wedding_city == "Chicago, Illinois"

    @pytest.mark.parametrize("url_name", ["admin:pages_page_changelist", "admin:pages_faqitem_changelist"])
    def test_changelists_load(self, admin_client, url_name):
        Page.objects.create(slug="our-story", title="Our Story")
        FaqItem.objects.create(question="Kids?")
        assert admin_client.get(reverse(url_name)).status_code == 200

    def test_page_admin_rejects_reserved_slug(self, admin_client):
        response = admin_client.post(
            reverse("admin:pages_page_add"),
            {"title": "RSVP", "slug": "rsvp", "body": "", "nav_label": "", "nav_order": 0, "show_in_nav": "on"},
        )
        assert response.status_code == 200
        assert "fixed route" in response.content.decode()
        assert not Page.objects.exists()
