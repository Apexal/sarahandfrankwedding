"""Public site: the password gate, the welcome page, and the guest-facing pages."""

from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.html import escape

from guests.models import Event, Household
from guests.session import RSVP_HOUSEHOLD_SESSION_KEY
from pages.middleware import SESSION_UNLOCK_KEY, SitePasswordMiddleware
from pages.models import FaqItem, Page, WeddingSettings
from pages.views import STALE_LINK_MESSAGE, WRONG_PASSWORD_MESSAGE

PASSWORD = "Willow2027"
WELCOME = reverse("pages:welcome")


def configure(**fields) -> WeddingSettings:
    wedding = WeddingSettings.load()
    for name, value in fields.items():
        setattr(wedding, name, value)
    wedding.save()
    return wedding


def unlock(client: Client, password: str = PASSWORD, **data):
    return client.post(WELCOME, {"password": password, **data})


def set_session_household(client: Client, household: Household) -> None:
    session = client.session
    session[RSVP_HOUSEHOLD_SESSION_KEY] = household.pk
    session.save()


@pytest.fixture
def gated(db):
    return configure(site_password=PASSWORD)


@pytest.mark.django_db
class TestSitePasswordMiddleware:
    def test_gate_is_a_noop_while_the_password_is_blank(self, client):
        assert client.get("/").status_code == 200
        assert client.get(reverse("pages:schedule")).status_code == 200

    def test_locked_visitor_is_redirected_to_welcome_with_next(self, client, gated):
        response = client.get("/schedule/?tab=day")
        assert response.status_code == 302
        assert response.url == "/welcome/?next=%2Fschedule%2F%3Ftab%3Dday"

    def test_htmx_request_gets_a_client_side_redirect(self, client, gated):
        response = client.get("/", HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert response.headers["HX-Redirect"] == "/welcome/?next=%2F"

    def test_staff_and_superusers_bypass_the_gate(self, client, admin_client, staff_user, gated):
        assert admin_client.get("/").status_code == 200
        client.force_login(staff_user)
        assert client.get("/").status_code == 200

    def test_ordinary_users_do_not_bypass_the_gate(self, user_client, gated):
        assert user_client.get("/").status_code == 302

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            ("/health/", 200),
            ("/robots.txt", 200),
            ("/favicon.ico", 204),
            ("/welcome/", 200),
            ("/accounts/login/", 200),
            ("/admin/login/", 302),  # secure_admin_login hands off to allauth, not to the gate
        ],
    )
    def test_exempt_routes_stay_reachable_while_locked(self, client, gated, path, status):
        response = client.get(path)
        assert response.status_code == status
        if status == 302:
            assert response.url.startswith("/accounts/login/")

    def test_static_and_media_paths_are_exempt(self):
        middleware = SitePasswordMiddleware(lambda request: None)
        for path in ("/static/img/hero.svg", "/media/uploads/x.jpg"):
            request = RequestFactory().get(path)
            request.resolver_match = None
            assert middleware._route_is_exempt(request), path
        request = RequestFactory().get("/schedule/")
        request.resolver_match = None
        assert not middleware._route_is_exempt(request)

    def test_health_is_exempt_before_the_database_is_consulted(self, client, gated, monkeypatch):
        monkeypatch.setattr(WeddingSettings, "load", classmethod(lambda cls: pytest.fail("touched the database")))
        assert client.get("/health/").status_code == 200

    def test_rotating_the_password_relocks_an_unlocked_session(self, client, gated):
        unlock(client)
        assert client.get("/").status_code == 200
        configure(site_password="Oak2027")
        assert client.get("/").status_code == 302
        unlock(client, "oak2027")
        assert client.get("/").status_code == 200

    def test_prose_pages_are_gated_too(self, client, gated):
        Page.objects.create(slug="our-story", title="Our Story", published=True)
        assert client.get("/our-story/").status_code == 302


@pytest.mark.django_db
class TestWelcome:
    def test_redirects_home_when_the_gate_is_off(self, client):
        response = client.get(WELCOME)
        assert response.status_code == 302
        assert response.url == reverse("pages:home")

    def test_renders_the_form_with_the_contact_email(self, client, gated):
        configure(contact_email="hello@example.com")
        html = client.get(WELCOME).content.decode()
        assert "The password is on your invitation." in html
        assert "Can't find it? Email us at" in html
        assert 'href="mailto:hello@example.com"' in html
        assert '<form method="post" action="/welcome/"' in html
        assert 'name="password"' in html

    def test_wrong_password_rerenders_with_the_error(self, client, gated):
        response = unlock(client, "nope")
        assert response.status_code == 200
        assert escape(WRONG_PASSWORD_MESSAGE) in response.content.decode()
        assert client.get("/").status_code == 302

    def test_right_password_unlocks_and_honours_a_safe_next(self, client, gated):
        response = unlock(client, "  willow2027 ", next="/schedule/")
        assert response.status_code == 302
        assert response.url == "/schedule/"
        assert client.get("/schedule/").status_code == 200
        assert client.session[SESSION_UNLOCK_KEY] == WeddingSettings.load().site_password_fingerprint

    @pytest.mark.parametrize("bad_next", ["https://evil.example/", "//evil.example/x", "javascript:alert(1)"])
    def test_an_unsafe_next_falls_back_to_home(self, client, gated, bad_next):
        response = unlock(client, next=bad_next)
        assert response.status_code == 302
        assert response.url == reverse("pages:home")

    def test_the_link_parameter_unlocks(self, client, gated):
        response = client.get(f"{WELCOME}?k=WILLOW2027&next=/faq/")
        assert response.status_code == 302
        assert response.url == "/faq/"
        assert client.get("/").status_code == 200

    def test_a_stale_link_shows_the_form_and_does_not_unlock(self, client, gated):
        response = client.get(f"{WELCOME}?k=old-password")
        assert response.status_code == 200
        assert escape(STALE_LINK_MESSAGE) in response.content.decode()
        assert client.get("/").status_code == 302

    def test_an_unlocked_visitor_is_sent_on(self, client, gated):
        unlock(client)
        response = client.get(f"{WELCOME}?next=/registry/")
        assert response.status_code == 302
        assert response.url == "/registry/"

    @override_settings(TESTING=False)
    def test_the_form_is_rate_limited_per_ip(self, client, gated):
        for _ in range(10):
            assert unlock(client, "nope").status_code == 200
        assert unlock(client, "nope").status_code == 429
        # GETs never burn the budget.
        assert client.get(WELCOME).status_code == 200


@pytest.mark.django_db
class TestHome:
    def test_shows_names_date_countdown_and_hero(self, client):
        wedding_date = timezone.localdate() + timedelta(days=100)
        configure(couple_names="Sarah & Frank", wedding_date=wedding_date, wedding_city="Chicago, Illinois")
        html = client.get("/").content.decode()
        assert "Sarah &amp; Frank" in html
        assert f'<time datetime="{wedding_date:%Y-%m-%d}">{date_format(wedding_date, "l,")}' in html
        assert date_format(wedding_date, "F j, Y") in html
        assert "100 days to go" in html
        assert "Chicago, Illinois" in html
        assert "img/hero.svg" in html
        assert "Details to follow." in html

    @pytest.mark.parametrize(("offset", "expected"), [(1, "Tomorrow!"), (0, "Today's the day!"), (-3, "We did it!")])
    def test_countdown_edges(self, client, offset, expected):
        configure(wedding_date=timezone.localdate() + timedelta(days=offset))
        assert expected in client.get("/").content.decode()

    def test_rsvp_call_to_action_only_when_open(self, client):
        assert 'href="/rsvp/"' not in client.get("/").content.decode()
        configure(rsvp_open=True)
        assert 'href="/rsvp/"' in client.get("/").content.decode()

    def test_schedule_link_replaces_details_to_follow(self, client):
        Event.objects.create(name="Ceremony", slug="ceremony", starts_at=timezone.now())
        html = client.get("/").content.decode()
        assert "Details to follow." not in html
        assert f'href="{reverse("pages:schedule")}"' in html

    def test_a_private_only_schedule_still_reads_details_to_follow(self, client):
        Event.objects.create(name="Rehearsal", slug="rehearsal", starts_at=timezone.now(), is_private=True)
        assert "Details to follow." in client.get("/").content.decode()

    def test_footer_carries_the_contact_email(self, client):
        configure(contact_email="hello@example.com")
        html = client.get("/").content.decode()
        assert 'href="mailto:hello@example.com"' in html


@pytest.mark.django_db
class TestNav:
    def test_order_is_home_schedule_pages_then_fixed_routes(self, client):
        Page.objects.create(slug="travel", title="Travel & Stay", nav_label="Travel", nav_order=2, published=True)
        Page.objects.create(slug="our-story", title="Our Story", nav_order=1, published=True)
        Page.objects.create(slug="draft", title="Draft Page", published=False)
        Page.objects.create(slug="hidden", title="Hidden Page", published=True, show_in_nav=False)
        html = client.get("/").content.decode()
        labels = (">Home<", ">Schedule<", ">Our Story<", ">Travel<", ">Registry<", ">FAQ<")
        positions = [html.index(label) for label in labels]
        assert positions == sorted(positions)
        assert "Draft Page" not in html
        assert "Hidden Page" not in html
        assert ">Photos<" not in html
        assert ">Staff<" not in html

    def test_photos_link_appears_once_the_album_is_set(self, client):
        configure(photo_album_url="https://photos.app.goo.gl/example")
        html = client.get("/").content.decode()
        assert ">Photos<" in html
        assert f'href="{reverse("pages:photos")}"' in html

    def test_rsvp_link_in_nav_only_when_open(self, client):
        assert 'href="/rsvp/" class="nav-link"' not in client.get("/").content.decode()
        configure(rsvp_open=True)
        assert 'href="/rsvp/" class="nav-link"' in client.get("/").content.decode()

    def test_staff_menu_only_for_staff(self, user_client, admin_client):
        assert ">Staff<" not in user_client.get("/").content.decode()
        html = admin_client.get("/").content.decode()
        assert ">Staff<" in html
        assert f'href="{reverse("admin:index")}"' in html
        assert 'href="/manage/"' in html
        assert f'href="{reverse("account_logout")}"' in html

    def test_current_page_is_marked(self, client):
        Page.objects.create(slug="our-story", title="Our Story", published=True)
        assert 'aria-current="page">Schedule<' in client.get("/schedule/").content.decode()
        assert 'aria-current="page">Our Story<' in client.get("/our-story/").content.decode()
        assert 'aria-current="page">Our Story<' not in client.get("/faq/").content.decode()

    def test_context_processor_exposes_nav_pages(self, client):
        shown = Page.objects.create(slug="our-story", title="Our Story", published=True)
        Page.objects.create(slug="draft", title="Draft", published=False)
        response = client.get("/")
        assert list(response.context["nav_pages"]) == [shown]
        assert response.context["wedding"].pk == 1


@pytest.mark.django_db
class TestSchedule:
    @pytest.fixture
    def events(self):
        starts = datetime(2027, 9, 18, 20, 0, tzinfo=UTC)
        return {
            "ceremony": Event.objects.create(
                name="Ceremony",
                slug="ceremony",
                starts_at=starts,
                ends_at=starts + timedelta(hours=1),
                venue_name="St. Mary's",
                address="1 Church St\nChicago, IL",
                dress_code="Cocktail attire",
                description="<p>Bring <strong>sunscreen</strong>.</p>",
                order=2,
            ),
            "rehearsal": Event.objects.create(
                name="Rehearsal Dinner",
                slug="rehearsal",
                starts_at=starts - timedelta(days=1),
                is_private=True,
                order=1,
            ),
        }

    @pytest.fixture
    def invited(self, events):
        household = Household.objects.create(name="The Joneses")
        household.invited_events.set([events["ceremony"], events["rehearsal"]])
        return household

    @pytest.fixture
    def uninvited(self, events):
        household = Household.objects.create(name="The Smiths")
        household.invited_events.set([events["ceremony"]])
        return household

    def test_private_event_is_hidden_without_a_session_household(self, client, events):
        html = client.get(reverse("pages:schedule")).content.decode()
        assert "Ceremony" in html
        assert "Rehearsal Dinner" not in html

    def test_private_event_is_hidden_from_an_uninvited_household(self, client, events, uninvited):
        set_session_household(client, uninvited)
        html = client.get(reverse("pages:schedule")).content.decode()
        assert "Ceremony" in html
        assert "Rehearsal Dinner" not in html

    def test_private_event_is_shown_to_an_invited_household(self, client, events, invited):
        set_session_household(client, invited)
        html = client.get(reverse("pages:schedule")).content.decode()
        assert html.index("Rehearsal Dinner") < html.index("Ceremony")  # ordered by `order`

    def test_renders_details_and_description(self, client, events):
        html = client.get(reverse("pages:schedule")).content.decode()
        starts_local = timezone.localtime(events["ceremony"].starts_at)
        ends_local = starts_local + timedelta(hours=1)
        assert date_format(starts_local, "F j, Y") in html  # the subtitle's date span
        assert date_format(starts_local, "l") in html
        if date_format(starts_local, "A") == date_format(ends_local, "A"):
            assert f"{date_format(starts_local, 'g:i')} –" in html
        else:
            assert date_format(starts_local, "g:i A") in html
        assert date_format(ends_local, "g:i A") in html
        assert "St. Mary&#x27;s" in html
        assert "1 Church St" in html
        assert "Cocktail attire" in html
        assert '<div class="ck-content"><p>Bring <strong>sunscreen</strong>.</p></div>' in html

    def test_empty_schedule_reads_details_to_follow(self, client):
        assert "Details to follow." in client.get(reverse("pages:schedule")).content.decode()


@pytest.mark.django_db
class TestRegistry:
    def test_links_render_as_buttons(self, client):
        configure(registry_links=[{"label": "Zola", "url": "https://zola.com/us"}])
        html = client.get(reverse("pages:registry")).content.decode()
        assert '<a href="https://zola.com/us" class="button button-outline w-full">Zola</a>' in html

    def test_no_links_reads_details_to_follow(self, client):
        assert "Details to follow." in client.get(reverse("pages:registry")).content.decode()


@pytest.mark.django_db
class TestFaq:
    def test_lists_published_items_with_answers_in_ck_content(self, client):
        FaqItem.objects.create(question="Can I bring kids?", answer="<p>We love them, but <em>no</em>.</p>", order=2)
        FaqItem.objects.create(question="Parking?", answer="<p>Plenty.</p>", order=1)
        FaqItem.objects.create(question="Secret?", answer="<p>Hidden.</p>", published=False)
        html = client.get(reverse("pages:faq")).content.decode()
        assert html.index("Parking?") < html.index("Can I bring kids?")
        assert '<dd class="ck-content mt-3"><p>We love them, but <em>no</em>.</p></dd>' in html
        assert "Secret?" not in html
        assert "collapse" not in html  # a plain list, nothing behind a tap


@pytest.mark.django_db
class TestPhotos:
    def test_404_without_an_album(self, client):
        assert client.get(reverse("pages:photos")).status_code == 404

    def test_big_button_and_note(self, client):
        configure(photo_album_url="https://photos.app.goo.gl/example", photo_album_note="You'll need a Google account.")
        html = client.get(reverse("pages:photos")).content.decode()
        assert 'href="https://photos.app.goo.gl/example"' in html
        assert "Open the album" in html
        assert "You&#x27;ll need a Google account." in html


@pytest.mark.django_db
class TestPage:
    def test_published_page_renders_its_body(self, client):
        Page.objects.create(slug="our-story", title="Our Story", body="<p>We met in <b>2019</b>.</p>", published=True)
        response = client.get("/our-story/")
        assert response.status_code == 200
        html = response.content.decode()
        assert "<title>" in html and "Our Story" in html
        assert '<div class="ck-content"><p>We met in <b>2019</b>.</p></div>' in html

    def test_unpublished_page_404s(self, client):
        Page.objects.create(slug="our-story", title="Our Story", published=False)
        assert client.get("/our-story/").status_code == 404

    def test_unknown_slug_404s(self, client):
        assert client.get("/nope/").status_code == 404
