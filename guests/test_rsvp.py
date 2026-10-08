"""RSVP slice: the name matcher, the form builder, the flow, open/closed states, and the check command."""

from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from guests.forms.rsvp import (
    CONDITIONAL_REQUIRED,
    PLUS_ONE_NAME_REQUIRED,
    attendance_field_name,
    build_rsvp_form,
    plus_one_field_name,
    question_field_name,
)
from guests.matching import find_household_by_code, find_households, normalize_invite_code
from guests.models import Event, Guest, Household, Rsvp, RsvpAnswer, RsvpQuestion
from guests.nicknames import are_nicknames, nicknames_for
from guests.session import RSVP_CANDIDATES_SESSION_KEY, RSVP_HOUSEHOLD_SESSION_KEY
from messaging.models import OutboundEmail
from pages.models import WeddingSettings

START = reverse("rsvp:start")
LOOKUP = reverse("rsvp:lookup")
RESPOND = reverse("rsvp:respond")
DONE = reverse("rsvp:done")
RESET = reverse("rsvp:reset")
CLOSED_MESSAGE = "RSVPs have closed for now."
INTRO = "Tell us who is coming."


def select_url(household: Household) -> str:
    return reverse("rsvp:select", args=[household.pk])


def configure(**fields) -> WeddingSettings:
    wedding = WeddingSettings.load()
    for name, value in fields.items():
        setattr(wedding, name, value)
    wedding.save()
    return WeddingSettings.load()


def set_session_household(client: Client, household: Household) -> None:
    session = client.session
    session[RSVP_HOUSEHOLD_SESSION_KEY] = household.pk
    session.save()


def set_session_candidates(client: Client, households: list[Household]) -> None:
    session = client.session
    session[RSVP_CANDIDATES_SESSION_KEY] = [household.pk for household in households]
    session.save()


@pytest.fixture
def wedding(db):
    return configure(
        couple_names="Sarah & Frank",
        contact_email="hello@example.com",
        rsvp_open=True,
        rsvp_deadline=timezone.localdate() + timedelta(days=30),
        rsvp_intro=f"<p>{INTRO}</p>",
        rsvp_closed_message=f"<p>{CLOSED_MESSAGE}</p>",
    )


@pytest.fixture
def events(wedding):
    starts = timezone.now() + timedelta(days=200)
    return {
        "rehearsal": Event.objects.create(
            name="Rehearsal dinner", slug="rehearsal", starts_at=starts - timedelta(days=1), is_private=True, order=0
        ),
        "ceremony": Event.objects.create(name="Ceremony", slug="ceremony", starts_at=starts, order=1),
        "reception": Event.objects.create(name="Reception", slug="reception", starts_at=starts, order=2),
    }


@pytest.fixture
def household(events):
    household = Household.objects.create(name="The Joneses", email="jones@example.com")
    household.invited_events.set([events["ceremony"], events["reception"]])
    Guest.objects.create(household=household, first_name="Robert", last_name="Jones", also_known_as="Bob, Bobby")
    Guest.objects.create(household=household, first_name="Carol", last_name="Jones", sort_order=1)
    return household


@pytest.fixture
def guests(household):
    robert, carol = household.guests.order_by("sort_order")
    return {"robert": robert, "carol": carol}


@pytest.fixture
def plus_one(household):
    return Guest.objects.create(household=household, is_plus_one=True, sort_order=2)


@pytest.fixture
def questions(events):
    reception = events["reception"]
    return {
        "meal": RsvpQuestion.objects.create(
            label="Meal choice",
            kind=RsvpQuestion.Kind.SINGLE_CHOICE,
            choices=["Beef", "Fish", "Vegetarian"],
            scope=RsvpQuestion.Scope.PER_GUEST,
            only_if_attending=reception,
            required=True,
            order=1,
        ),
        "dietary": RsvpQuestion.objects.create(
            label="Dietary restrictions",
            kind=RsvpQuestion.Kind.SHORT_TEXT,
            scope=RsvpQuestion.Scope.PER_GUEST,
            only_if_attending=reception,
            order=2,
        ),
        "song": RsvpQuestion.objects.create(
            label="A song that will get you dancing",
            kind=RsvpQuestion.Kind.SHORT_TEXT,
            scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
            only_if_attending=reception,
            order=3,
        ),
        "hotel": RsvpQuestion.objects.create(
            label="Staying at the hotel block?",
            kind=RsvpQuestion.Kind.YES_NO,
            scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
            order=4,
        ),
        "activities": RsvpQuestion.objects.create(
            label="Weekend activities",
            kind=RsvpQuestion.Kind.MULTI_CHOICE,
            choices=["Brunch", "Hike"],
            scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
            order=5,
        ),
        "notes": RsvpQuestion.objects.create(
            label="Anything else?",
            kind=RsvpQuestion.Kind.LONG_TEXT,
            scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
            order=6,
        ),
        "retired": RsvpQuestion.objects.create(
            label="Old question",
            kind=RsvpQuestion.Kind.SHORT_TEXT,
            scope=RsvpQuestion.Scope.PER_HOUSEHOLD,
            active=False,
        ),
    }


def full_answers(household, events, *, attending=True, **extra) -> dict:
    """POST data answering yes (or no) to every required event for every guest."""
    data = {}
    for guest in household.guests.all():
        for key in ("ceremony", "reception"):
            data[attendance_field_name(guest.pk, events[key].pk)] = "yes" if attending else "no"
    data.update(extra)
    return data


# --- Matcher ----------------------------------------------------------------------------


@pytest.fixture
def directory(db):
    """A small guest list with the shapes the matcher has to handle."""
    made = {}

    def add(name, guests, plus_one=False):
        household = Household.objects.create(name=name)
        for index, (first, last, *aka) in enumerate(guests):
            Guest.objects.create(
                household=household,
                first_name=first,
                last_name=last,
                also_known_as=aka[0] if aka else "",
                sort_order=index,
            )
        if plus_one:
            Guest.objects.create(household=household, is_plus_one=True, sort_order=len(guests))
        made[name] = household
        return household

    add("The Joneses", [("Robert", "Jones", "Bob, Bobby"), ("Carol", "Jones")])
    add("The Smith-Garcias", [("José", "Smith-Garcia"), ("María", "Smith-Garcia")])
    add("The Lees", [("Ann", "Lee")])
    add("The Leeds", [("Ann", "Leeds")])
    add("Anderson & Guest", [("Elizabeth", "Anderson")], plus_one=True)
    add("The Williams", [("Bill", "Williams")])
    add("The Kowalskis", [("Mary Ann", "Kowalski")])
    return made


class TestNicknames:
    @pytest.mark.parametrize(
        ("first", "second", "expected"),
        [
            ("robert", "bob", True),
            ("bob", "robert", True),
            ("bob", "bobby", True),
            ("william", "liam", True),
            ("chris", "christine", True),
            ("christopher", "christine", False),
            ("robert", "robert", False),
            ("robert", "carol", False),
            ("", "bob", False),
        ],
    )
    def test_are_nicknames(self, first, second, expected):
        assert are_nicknames(first, second) is expected

    def test_nicknames_for_excludes_itself_and_spans_groups(self):
        assert "bob" in nicknames_for("robert")
        assert "robert" not in nicknames_for("robert")
        assert {"christopher", "christine", "christina"} <= nicknames_for("chris")


@pytest.mark.django_db
class TestFindHouseholds:
    @pytest.mark.parametrize(
        ("first", "last", "expected"),
        [
            ("Robert", "Jones", ["The Joneses"]),
            ("robert", "JONES", ["The Joneses"]),
            ("Bob", "Jones", ["The Joneses"]),  # nickname: typed short form, stored formal
            ("William", "Williams", ["The Williams"]),  # nickname: typed formal, stored short form
            ("Bobby", "Jones", ["The Joneses"]),  # also_known_as
            ("Carol", "Jones", ["The Joneses"]),
            ("Jose", "Smith-Garcia", ["The Smith-Garcias"]),  # accent stripped both ways
            ("María", "Garcia", ["The Smith-Garcias"]),  # one token of a hyphenated surname
            ("Jose", "Smith Garcia", ["The Smith-Garcias"]),
            ("Robet", "Jones", ["The Joneses"]),  # first-name typo
            ("Elizabeth", "Andersen", ["Anderson & Guest"]),  # last-name typo, plus-one with a blank name
            ("Liz", "Anderson", ["Anderson & Guest"]),
            ("Mary", "Kowalski", ["The Kowalskis"]),  # one token of a two-word first name
            ("Ann", "Lee", ["The Lees"]),  # ratio, not partial_ratio: Lee is not Leeds
            ("Ann", "Leeds", ["The Leeds"]),
            ("Nobody", "Jones", []),
            ("Robert", "Nobody", []),
            ("", "Jones", []),
            ("Robert", "", []),
        ],
    )
    def test_table(self, directory, first, last, expected):
        assert [household.name for household in find_households(first, last)] == expected

    def test_ranks_exact_first_and_caps_at_three(self, db):
        for name, first in (("Smith B", "Jon"), ("Smith C", "Jack"), ("Smith A", "John"), ("Smith D", "Johnny")):
            household = Household.objects.create(name=name)
            Guest.objects.create(household=household, first_name=first, last_name="Smith")
        found = [household.name for household in find_households("John", "Smith")]
        assert len(found) == 3
        assert found[0] == "Smith A"

    def test_blank_plus_one_never_matches_on_its_own(self, directory):
        assert find_households("", "") == []
        assert find_households("Guest", "") == []


@pytest.mark.django_db
class TestFindHouseholdByCode:
    def test_case_and_whitespace_insensitive(self, household):
        code = household.invite_code
        assert find_household_by_code(f"  {code.lower()} ") == household
        assert find_household_by_code(f"{code[:3]}-{code[3:]}") == household
        assert find_household_by_code("") is None
        assert find_household_by_code("ZZZZZZ") is None

    def test_normalize(self):
        assert normalize_invite_code(" ab-c d e f ") == "ABCDEF"


# --- The form -------------------------------------------------------------------------


@pytest.mark.django_db
class TestBuildRsvpForm:
    def test_fields_follow_the_naming_contract(self, household, guests, events, questions, plus_one):
        form = build_rsvp_form(household)
        names = set(form.fields)
        for guest in (guests["robert"], guests["carol"], plus_one):
            assert attendance_field_name(guest.pk, events["ceremony"].pk) in names
            assert attendance_field_name(guest.pk, events["reception"].pk) in names
            assert question_field_name(questions["meal"].pk, guest.pk) in names
        # Rehearsal dinner is not an invited event.
        assert attendance_field_name(guests["robert"].pk, events["rehearsal"].pk) not in names
        assert plus_one_field_name(plus_one.pk) in names
        assert plus_one_field_name(guests["robert"].pk) not in names
        for key in ("song", "hotel", "activities", "notes"):
            assert question_field_name(questions[key].pk) in names
        assert question_field_name(questions["retired"].pk) not in names
        assert "contact_email" in names
        assert form.initial["contact_email"] == "jones@example.com"
        assert [section.guest for section in form.sections] == [guests["robert"], guests["carol"], plus_one]

    def test_conditional_question_on_uninvited_event_is_not_asked(self, household, events, questions):
        questions["meal"].only_if_attending = events["rehearsal"]
        questions["meal"].save()
        form = build_rsvp_form(household)
        assert not any(name.startswith(f"q_{questions['meal'].pk}_") for name in form.fields)

    def test_prefills_from_existing_rows(self, household, guests, events, questions):
        Rsvp.objects.create(guest=guests["robert"], event=events["ceremony"], attending=True)
        Rsvp.objects.create(guest=guests["robert"], event=events["reception"], attending=False)
        RsvpAnswer.objects.create(question=questions["hotel"], household=household, value=True)
        RsvpAnswer.objects.create(question=questions["activities"], household=household, value=["Hike"])
        RsvpAnswer.objects.create(question=questions["meal"], household=household, guest=guests["robert"], value="Fish")
        form = build_rsvp_form(household)
        assert form.initial[attendance_field_name(guests["robert"].pk, events["ceremony"].pk)] == "yes"
        assert form.initial[attendance_field_name(guests["robert"].pk, events["reception"].pk)] == "no"
        assert attendance_field_name(guests["carol"].pk, events["ceremony"].pk) not in form.initial
        assert form.initial[question_field_name(questions["hotel"].pk)] == "yes"
        assert form.initial[question_field_name(questions["activities"].pk)] == ["Hike"]
        assert form.initial[question_field_name(questions["meal"].pk, guests["robert"].pk)] == "Fish"

    def test_events_past_their_own_deadline_are_left_out(self, household, events):
        events["reception"].rsvp_deadline = timezone.localdate() - timedelta(days=1)
        events["reception"].save()
        form = build_rsvp_form(household)
        assert form.events == [events["ceremony"]]


# --- The flow -------------------------------------------------------------------------


@pytest.mark.django_db
class TestLookupAndSelect:
    def test_start_shows_intro_and_both_forms(self, client, wedding):
        response = client.get(START)
        assert response.status_code == 200
        assert INTRO in response.text
        assert 'name="first_name"' in response.text
        assert 'name="code"' in response.text
        assert "Have a code? Enter it instead" in response.text

    def test_lookup_offers_household_names_and_first_names_only(self, client, household):
        response = client.post(LOOKUP, {"first_name": "Bob", "last_name": "Jones"})
        assert response.status_code == 200
        assert "The Joneses" in response.text
        assert "Robert &amp; Carol" in response.text
        assert "jones@example.com" not in response.text
        assert select_url(household) in response.text
        assert client.session[RSVP_CANDIDATES_SESSION_KEY] == [household.pk]

    def test_lookup_with_no_match_says_so_with_the_contact_email(self, client, household):
        response = client.post(LOOKUP, {"first_name": "Zelda", "last_name": "Nobody"})
        assert response.status_code == 200
        assert "We couldn&#x27;t find you" in response.text or "We couldn't find you" in response.text
        assert "hello@example.com" in response.text
        assert client.session[RSVP_CANDIDATES_SESSION_KEY] == []

    def test_lookup_with_blank_names_rerenders_the_start_form(self, client, household):
        response = client.post(LOOKUP, {"first_name": "", "last_name": "Jones"})
        assert response.status_code == 200
        assert "Please enter both your first and last name." in response.text

    def test_lookup_get_is_not_allowed(self, client, wedding):
        assert client.get(LOOKUP).status_code == 405

    def test_select_from_candidates_sets_the_session(self, client, household):
        set_session_candidates(client, [household])
        response = client.post(select_url(household))
        assert response.status_code == 302
        assert response.url == RESPOND
        assert client.session[RSVP_HOUSEHOLD_SESSION_KEY] == household.pk
        assert RSVP_CANDIDATES_SESSION_KEY not in client.session

    def test_forged_household_id_is_rejected(self, client, household, events):
        other = Household.objects.create(name="The Others")
        set_session_candidates(client, [other])
        assert client.post(select_url(household)).status_code == 403
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session

    def test_select_without_a_lookup_is_rejected(self, client, household):
        assert client.post(select_url(household)).status_code == 403

    def test_code_path_bypasses_the_matcher(self, client, household, monkeypatch):
        monkeypatch.setattr(
            "guests.views.rsvp.find_households", lambda *a, **k: pytest.fail("the matcher ran for a code")
        )
        response = client.post(LOOKUP, {"code": f" {household.invite_code.lower()} "})
        assert response.status_code == 302
        assert response.url == RESPOND
        assert client.session[RSVP_HOUSEHOLD_SESSION_KEY] == household.pk

    def test_unknown_code_is_a_miss(self, client, household):
        response = client.post(LOOKUP, {"code": "ZZZZZZ"})
        assert response.status_code == 200
        assert "hello@example.com" in response.text
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session

    def test_preseeded_session_skips_lookup(self, client, household):
        set_session_household(client, household)
        response = client.get(START)
        assert response.status_code == 302
        assert response.url == RESPOND
        response = client.get(RESPOND)
        assert response.status_code == 200
        assert "Not The Joneses? Start over" in response.text

    def test_respond_and_done_without_a_household_go_to_start(self, client, wedding):
        assert client.get(RESPOND).url == START
        assert client.get(DONE).url == START

    def test_reset_clears_the_session(self, client, household):
        set_session_household(client, household)
        set_session_candidates(client, [household])
        response = client.post(RESET)
        assert response.url == START
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session
        assert RSVP_CANDIDATES_SESSION_KEY not in client.session

    def test_stale_household_in_session_is_forgotten(self, client, household):
        set_session_household(client, household)
        household.delete()
        assert client.get(START).status_code == 200
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session


@pytest.mark.django_db
class TestRespond:
    def test_full_flow_saves_rows_and_queues_both_emails(self, client, settings, household, guests, events, questions):
        settings.APP_BASE_URL = "https://sarahandfrank.wedding"
        client.post(LOOKUP, {"first_name": "Robert", "last_name": "Jones"})
        client.post(select_url(household))
        assert client.get(RESPOND).status_code == 200

        data = full_answers(household, events)
        data[attendance_field_name(guests["carol"].pk, events["reception"].pk)] = "no"
        data[question_field_name(questions["meal"].pk, guests["robert"].pk)] = "Fish"
        data[question_field_name(questions["dietary"].pk, guests["robert"].pk)] = "No nuts"
        data[question_field_name(questions["song"].pk)] = "September"
        data[question_field_name(questions["hotel"].pk)] = "no"
        data[question_field_name(questions["activities"].pk)] = ["Brunch", "Hike"]
        data[question_field_name(questions["notes"].pk)] = "Can't wait!"
        data["contact_email"] = "jones@example.com"
        response = client.post(RESPOND, data)
        assert response.status_code == 302, response.context["form"].errors
        assert response.url == DONE

        rsvps = {(r.guest_id, r.event.slug): r for r in Rsvp.objects.filter(guest__household=household)}
        assert len(rsvps) == 4
        assert rsvps[(guests["carol"].pk, "reception")].attending is False
        assert rsvps[(guests["robert"].pk, "reception")].attending is True
        assert all(r.source == Rsvp.Source.GUEST for r in rsvps.values())

        answers = {(a.question_id, a.guest_id): a.value for a in RsvpAnswer.objects.filter(household=household)}
        assert answers == {
            (questions["meal"].pk, guests["robert"].pk): "Fish",
            (questions["dietary"].pk, guests["robert"].pk): "No nuts",
            (questions["song"].pk, None): "September",
            (questions["hotel"].pk, None): False,
            (questions["activities"].pk, None): ["Brunch", "Hike"],
            (questions["notes"].pk, None): "Can't wait!",
        }

        household.refresh_from_db()
        assert household.responded_at is not None

        emails = {e.template_key: e for e in OutboundEmail.objects.all()}
        assert set(emails) == {"rsvp_confirmation", "rsvp_received_internal"}
        confirmation = emails["rsvp_confirmation"]
        assert confirmation.to_email == "jones@example.com"
        assert confirmation.reply_to_email == "hello@example.com"
        assert confirmation.status == OutboundEmail.DeliveryStatus.QUEUED
        assert "Robert Jones" in confirmation.body_html_rendered
        assert "Reception: no" in confirmation.body_html_rendered
        assert "Meal choice (Robert Jones): Fish" in confirmation.body_html_rendered
        assert "https://sarahandfrank.wedding/rsvp/" in confirmation.body_html_rendered
        internal = emails["rsvp_received_internal"]
        assert internal.to_email == "hello@example.com"
        assert internal.status == OutboundEmail.DeliveryStatus.QUEUED
        assert f"https://sarahandfrank.wedding/admin/guests/household/{household.pk}/change/" in (
            internal.body_html_rendered
        )
        assert "Carol Jones: Ceremony yes, Reception no" in internal.body_html_rendered
        assert "Staying at the hotel block?: No" in internal.body_html_rendered

        done = client.get(DONE)
        assert done.status_code == 200
        assert "We emailed a confirmation to jones@example.com" in done.text
        assert "Robert Jones" in done.text
        assert "September" in done.text
        assert "Not you? Start over" in done.text

    def test_blank_email_sends_no_confirmation(self, client, household, events):
        set_session_household(client, household)
        response = client.post(RESPOND, full_answers(household, events, contact_email=""), follow=True)
        assert response.status_code == 200
        assert "No confirmation email was sent" in response.text
        assert list(OutboundEmail.objects.values_list("template_key", flat=True)) == ["rsvp_received_internal"]
        household.refresh_from_db()
        assert household.email == "jones@example.com"

    def test_new_email_updates_the_household_and_clears_a_bounce(self, client, household, events):
        household.email_bounced_at = timezone.now()
        household.save()
        set_session_household(client, household)
        client.post(RESPOND, full_answers(household, events, contact_email="new@example.com"))
        household.refresh_from_db()
        assert household.email == "new@example.com"
        assert household.email_bounced_at is None
        assert OutboundEmail.objects.get(template_key="rsvp_confirmation").to_email == "new@example.com"

    def test_no_contact_email_means_no_internal_notice(self, client, household, events):
        configure(contact_email="")
        set_session_household(client, household)
        client.post(RESPOND, full_answers(household, events, contact_email="jones@example.com"))
        assert list(OutboundEmail.objects.values_list("template_key", flat=True)) == ["rsvp_confirmation"]
        assert OutboundEmail.objects.get().reply_to_email == ""

    def test_missing_attendance_rerenders_with_an_error_summary(self, client, household, guests, events):
        set_session_household(client, household)
        data = full_answers(household, events)
        del data[attendance_field_name(guests["carol"].pk, events["ceremony"].pk)]
        response = client.post(RESPOND, data)
        assert response.status_code == 200
        assert "A few things need your attention" in response.text
        assert "Carol Jones · Ceremony" in response.text
        assert Rsvp.objects.count() == 0
        assert OutboundEmail.objects.count() == 0

    def test_editing_preserves_prior_answers(self, client, household, guests, events, questions):
        set_session_household(client, household)
        first = full_answers(household, events, contact_email="jones@example.com")
        first[question_field_name(questions["meal"].pk, guests["robert"].pk)] = "Beef"
        first[question_field_name(questions["meal"].pk, guests["carol"].pk)] = "Fish"
        first[question_field_name(questions["song"].pk)] = "September"
        assert client.post(RESPOND, first).status_code == 302
        household.refresh_from_db()
        first_responded_at = household.responded_at

        page = client.get(RESPOND)
        form = page.context["form"]
        assert form.initial[attendance_field_name(guests["robert"].pk, events["ceremony"].pk)] == "yes"
        assert form.initial[question_field_name(questions["meal"].pk, guests["carol"].pk)] == "Fish"
        assert form.initial[question_field_name(questions["song"].pk)] == "September"

        second = dict(first)
        second[attendance_field_name(guests["carol"].pk, events["ceremony"].pk)] = "no"
        second[question_field_name(questions["meal"].pk, guests["carol"].pk)] = "Vegetarian"
        assert client.post(RESPOND, second).status_code == 302

        assert Rsvp.objects.filter(guest__household=household).count() == 4
        assert Rsvp.objects.get(guest=guests["carol"], event=events["ceremony"]).attending is False
        assert RsvpAnswer.objects.get(question=questions["meal"], guest=guests["carol"]).value == "Vegetarian"
        assert RsvpAnswer.objects.get(question=questions["meal"], guest=guests["robert"]).value == "Beef"
        assert RsvpAnswer.objects.get(question=questions["song"], guest=None).value == "September"
        household.refresh_from_db()
        assert household.responded_at == first_responded_at
        assert OutboundEmail.objects.filter(template_key="rsvp_confirmation").count() == 2

    def test_conditional_question_is_required_when_attending(self, client, household, guests, events, questions):
        set_session_household(client, household)
        data = full_answers(household, events)
        data[question_field_name(questions["meal"].pk, guests["carol"].pk)] = "Beef"
        response = client.post(RESPOND, data)
        assert response.status_code == 200
        errors = response.context["form"].errors
        assert errors[question_field_name(questions["meal"].pk, guests["robert"].pk)] == [CONDITIONAL_REQUIRED]
        assert question_field_name(questions["meal"].pk, guests["carol"].pk) not in errors

    def test_conditional_question_is_ignored_when_declining(self, client, household, guests, events, questions):
        RsvpAnswer.objects.create(question=questions["meal"], household=household, guest=guests["robert"], value="Beef")
        RsvpAnswer.objects.create(question=questions["song"], household=household, value="September")
        set_session_household(client, household)
        data = full_answers(household, events, attending=False)
        data[question_field_name(questions["meal"].pk, guests["robert"].pk)] = "Fish"
        data[question_field_name(questions["song"].pk)] = "Ignored"
        data[question_field_name(questions["hotel"].pk)] = "yes"
        response = client.post(RESPOND, data)
        assert response.status_code == 302, response.context["form"].errors
        assert not RsvpAnswer.objects.filter(question=questions["meal"]).exists()
        assert not RsvpAnswer.objects.filter(question=questions["song"]).exists()
        assert RsvpAnswer.objects.get(question=questions["hotel"]).value is True
        assert Rsvp.objects.filter(guest__household=household, attending=False).count() == 4

    def test_household_question_asked_when_anyone_attends(self, client, household, guests, events, questions):
        set_session_household(client, household)
        data = full_answers(household, events, attending=False)
        data[attendance_field_name(guests["carol"].pk, events["reception"].pk)] = "yes"
        data[question_field_name(questions["meal"].pk, guests["carol"].pk)] = "Fish"
        data[question_field_name(questions["song"].pk)] = "September"
        assert client.post(RESPOND, data).status_code == 302
        assert RsvpAnswer.objects.get(question=questions["song"]).value == "September"

    def test_plus_one_name_required_only_when_attending(self, client, household, events, plus_one):
        set_session_household(client, household)
        data = full_answers(household, events)
        response = client.post(RESPOND, data)
        assert response.status_code == 200
        assert response.context["form"].errors[plus_one_field_name(plus_one.pk)] == [PLUS_ONE_NAME_REQUIRED]
        assert "Guest of The Joneses · their name" in response.text

        declined = dict(data)
        for key in ("ceremony", "reception"):
            declined[attendance_field_name(plus_one.pk, events[key].pk)] = "no"
        assert client.post(RESPOND, declined).status_code == 302
        plus_one.refresh_from_db()
        assert plus_one.full_name == ""

        named = dict(data, **{plus_one_field_name(plus_one.pk): "Sam Taylor"})
        assert client.post(RESPOND, named).status_code == 302
        plus_one.refresh_from_db()
        assert (plus_one.first_name, plus_one.last_name) == ("Sam", "Taylor")
        assert plus_one.last_name_normalized == "taylor"
        # Once named they are a regular guest: no name field on the next visit.
        assert plus_one_field_name(plus_one.pk) not in client.get(RESPOND).context["form"].fields

    def test_email_template_failure_does_not_lose_the_rsvp(self, client, household, events, monkeypatch):
        from messaging.services import EmailTemplateNotFoundError, EmailTemplateService

        def explode(self, **kwargs):
            raise EmailTemplateNotFoundError("gone")

        monkeypatch.setattr(EmailTemplateService, "send_email", explode)
        set_session_household(client, household)
        response = client.post(RESPOND, full_answers(household, events, contact_email="jones@example.com"))
        assert response.status_code == 302
        assert Rsvp.objects.filter(guest__household=household).count() == 4


# --- Open and closed --------------------------------------------------------------------


@pytest.mark.django_db
class TestOpenAndClosed:
    def test_switched_off_closes_every_step(self, client, household, events):
        configure(rsvp_open=False)
        for url in (START, DONE, RESPOND):
            response = client.get(url)
            assert response.status_code == 200
            assert CLOSED_MESSAGE in response.text
            assert 'name="first_name"' not in response.text
        assert CLOSED_MESSAGE in client.post(LOOKUP, {"first_name": "Robert", "last_name": "Jones"}).text
        set_session_candidates(client, [household])
        assert CLOSED_MESSAGE in client.post(select_url(household)).text
        assert RSVP_HOUSEHOLD_SESSION_KEY not in client.session

    def test_past_deadline_shows_the_summary_for_a_known_household(self, client, household, guests, events):
        Rsvp.objects.create(guest=guests["robert"], event=events["ceremony"], attending=True)
        configure(rsvp_deadline=timezone.localdate() - timedelta(days=1))
        set_session_household(client, household)
        response = client.get(RESPOND)
        assert response.status_code == 200
        assert CLOSED_MESSAGE in response.text
        assert "What we have for The Joneses" in response.text
        assert "Robert Jones" in response.text
        assert "No answer yet" in response.text
        assert 'name="' + attendance_field_name(guests["robert"].pk, events["ceremony"].pk) not in response.text

    def test_deadline_day_is_still_open(self, client, household):
        configure(rsvp_deadline=timezone.localdate())
        assert INTRO in client.get(START).text

    def test_event_deadline_overrides_a_passed_wedding_deadline(self, client, household, guests, events):
        configure(rsvp_deadline=timezone.localdate() - timedelta(days=1))
        events["ceremony"].rsvp_deadline = timezone.localdate() + timedelta(days=7)
        events["ceremony"].save()
        assert INTRO in client.get(START).text

        set_session_household(client, household)
        response = client.get(RESPOND)
        assert response.status_code == 200
        assert attendance_field_name(guests["robert"].pk, events["ceremony"].pk) in response.context["form"].fields
        assert attendance_field_name(guests["robert"].pk, events["reception"].pk) not in response.context["form"].fields
        assert "The deadline for Reception has passed" in response.text

        data = {attendance_field_name(guest.pk, events["ceremony"].pk): "yes" for guest in household.guests.all()}
        assert client.post(RESPOND, data).status_code == 302
        assert Rsvp.objects.filter(event=events["ceremony"]).count() == 2
        assert not Rsvp.objects.filter(event=events["reception"]).exists()

    def test_event_deadline_closes_a_household_invited_only_to_it(self, client, household, events):
        events["reception"].rsvp_deadline = timezone.localdate() - timedelta(days=1)
        events["reception"].save()
        household.invited_events.set([events["reception"]])
        set_session_household(client, household)
        assert CLOSED_MESSAGE in client.get(RESPOND).text
        # Others, invited to the ceremony, are unaffected.
        assert INTRO in Client().get(START).text

    def test_no_deadline_anywhere_means_open(self, client, household):
        configure(rsvp_deadline=None)
        assert INTRO in client.get(START).text


# --- The check command ------------------------------------------------------------------


@pytest.mark.django_db
class TestRsvpMatchCheck:
    def write(self, tmp_path, rows):
        path = tmp_path / "names.csv"
        lines = ["typed_first,typed_last,expected_household", *rows]
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_reports_ok_ambiguous_and_miss_and_fails_on_a_miss(self, directory, tmp_path, capsys):
        Guest.objects.create(household=directory["The Leeds"], first_name="Robert", last_name="Jones", sort_order=1)
        path = self.write(
            tmp_path,
            ["Bob,Jones,The Joneses", "Carol,Jones,The Joneses", "Ann,Leeds,The Lees", "Zed,Zed,", "Ann,Lee,"],
        )
        with pytest.raises(CommandError, match="2 row"):
            call_command("rsvp_match_check", str(path))
        out = capsys.readouterr().out
        assert "ambiguous Bob Jones" in out
        assert "ok        Carol Jones" in out
        assert "miss      Ann Leeds" in out
        assert "ok        Zed Zed" in out
        assert "miss      Ann Lee" in out
        assert "5 row(s): 2 ok, 1 ambiguous, 2 miss" in out

    def test_all_ok_exits_cleanly(self, directory, tmp_path, capsys):
        path = self.write(tmp_path, ["Liz,Anderson,Anderson & Guest", "Jose,Garcia,The Smith-Garcias"])
        call_command("rsvp_match_check", str(path))
        assert "2 row(s): 2 ok, 0 ambiguous, 0 miss" in capsys.readouterr().out

    def test_missing_column_is_an_error(self, db, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("first,last\nBob,Jones\n")
        with pytest.raises(CommandError, match="Missing column"):
            call_command("rsvp_match_check", str(path))
