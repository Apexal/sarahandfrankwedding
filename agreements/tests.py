import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from agreements.models import Agreement, AgreementVersion, UserAgreement
from agreements.services import REQUIRED_VERSIONS_CACHE_KEY, get_pending_versions, has_pending_agreements
from agreements.templatetags.agreement_filters import render_markdown


def make_agreement(key="terms-of-service", name="Terms of Service", *, publish=True, **kwargs):
    agreement = Agreement.objects.create(key=key, name=name, **kwargs)
    version = AgreementVersion.objects.create(
        agreement=agreement,
        content_markdown="# Terms\n\nYou agree to our terms.",
        effective_date=timezone.now().date(),
    )
    if publish:
        version.publish()
        agreement.refresh_from_db()
    return agreement


@pytest.fixture
def agreement(db):
    return make_agreement()


class TestVersionNumbering:
    def test_first_version_is_one(self, db):
        agreement = make_agreement(publish=False)
        assert agreement.versions.get().version == 1

    def test_subsequent_versions_increment(self, agreement):
        second = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="v2", effective_date=timezone.now().date()
        )
        assert second.version == 2

    def test_explicit_version_is_respected(self, agreement):
        explicit = AgreementVersion.objects.create(
            agreement=agreement, version=99, content_markdown="v99", effective_date=timezone.now().date()
        )
        assert explicit.version == 99

    def test_numbering_is_per_agreement(self, agreement):
        other = make_agreement(key="privacy-policy", name="Privacy Policy", publish=False)
        assert other.versions.get().version == 1

    def test_duplicate_version_is_rejected(self, agreement):
        with pytest.raises(IntegrityError):
            AgreementVersion.objects.create(
                agreement=agreement, version=1, content_markdown="dupe", effective_date=timezone.now().date()
            )


class TestPublish:
    def test_publish_sets_current_version(self, db):
        agreement = make_agreement(publish=False)
        version = agreement.versions.get()
        version.publish()
        agreement.refresh_from_db()
        assert agreement.current_version_id == version.pk

    def test_publish_records_publisher_and_time(self, db, staff_user):
        agreement = make_agreement(publish=False)
        version = agreement.versions.get()
        version.publish(published_by=staff_user)
        version.refresh_from_db()
        assert version.published_by == staff_user
        assert version.published_at is not None

    def test_is_current_reflects_latest_publish(self, agreement):
        first = agreement.current_version
        second = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="v2", effective_date=timezone.now().date()
        )
        second.publish()
        first.refresh_from_db()
        second.refresh_from_db()
        assert second.is_current
        assert not first.is_current


class TestReservedKeys:
    @pytest.mark.parametrize("key", ["pending", "history", "versions"])
    def test_reserved_keys_rejected(self, db, key):
        with pytest.raises(ValidationError):
            Agreement(key=key, name="Nope").full_clean()

    def test_ordinary_key_allowed(self, db):
        Agreement(key="privacy-policy", name="Privacy Policy").full_clean()


class TestPendingLookup:
    def test_no_agreements_means_nothing_pending(self, user):
        assert has_pending_agreements(user) is False
        assert get_pending_versions(user) == []

    def test_unaccepted_required_agreement_is_pending(self, user, agreement):
        assert has_pending_agreements(user) is True
        assert [v.pk for v in get_pending_versions(user)] == [agreement.current_version_id]

    def test_accepted_agreement_is_not_pending(self, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        assert has_pending_agreements(user) is False
        assert get_pending_versions(user) == []

    def test_optional_agreement_is_never_pending(self, user, db):
        make_agreement(key="newsletter", name="Newsletter", required=False)
        assert has_pending_agreements(user) is False

    def test_notice_only_agreement_is_never_pending(self, user, db):
        make_agreement(key="policy-notice", name="Policy Notice", requires_reacceptance=False)
        assert has_pending_agreements(user) is False

    def test_unpublished_agreement_is_never_pending(self, user, db):
        make_agreement(key="draft", name="Draft", publish=False)
        assert has_pending_agreements(user) is False

    def test_new_version_becomes_pending_again(self, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        second = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="v2", effective_date=timezone.now().date()
        )
        second.publish()
        assert has_pending_agreements(user) is True
        assert [v.pk for v in get_pending_versions(user)] == [second.pk]

    def test_acceptance_is_per_user(self, user, admin_user, agreement):
        UserAgreement.objects.create(user=admin_user, agreement_version=agreement.current_version)
        assert has_pending_agreements(user) is True

    def test_cache_is_invalidated_when_a_version_is_published(self, user, db):
        assert has_pending_agreements(user) is False  # primes the cache with "nothing required"
        make_agreement()
        assert has_pending_agreements(user) is True


class TestAgreementGate:
    def test_pending_agreement_redirects_to_acceptance_page(self, user_client, agreement):
        response = user_client.get(reverse("core:home"))
        assert response.status_code == 302
        assert response.url == reverse("agreements:pending")

    def test_gate_lifts_after_accepting(self, user_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        assert user_client.get(reverse("core:home")).status_code == 200

    def test_no_agreements_means_no_gate(self, user_client):
        assert user_client.get(reverse("core:home")).status_code == 200

    def test_anonymous_users_are_not_gated(self, client, agreement):
        assert client.get(reverse("core:home")).status_code == 200

    def test_staff_are_not_gated(self, client, staff_user, agreement):
        client.force_login(staff_user)
        assert client.get(reverse("core:home")).status_code == 200

    def test_agreements_app_is_exempt(self, user_client, agreement):
        # Otherwise the gate would redirect the page it redirects to.
        assert user_client.get(reverse("agreements:pending")).status_code == 200
        assert user_client.get(reverse("agreements:history")).status_code == 200

    def test_allauth_is_exempt(self, user_client, agreement):
        assert user_client.get(reverse("account_email")).status_code == 200

    def test_health_check_is_exempt(self, user_client, agreement):
        assert user_client.get("/health/").status_code == 200

    def test_stale_cache_cannot_trap_the_user_in_a_redirect_loop(self, user_client, db):
        # A cached ID whose row is gone makes the gate think something is pending while
        # the pending page has nothing to show. Without the cache bust in that view the
        # two bounce off each other until the TTL expires and the site is unusable.
        cache.set(REQUIRED_VERSIONS_CACHE_KEY, [999999], 3600)

        gated = user_client.get(reverse("core:home"))
        assert gated.status_code == 302
        assert gated.url == reverse("agreements:pending")

        bounced = user_client.get(reverse("agreements:pending"))
        assert bounced.status_code == 302
        assert bounced.url == reverse("core:home")

        assert user_client.get(reverse("core:home")).status_code == 200

    def test_htmx_request_gets_client_redirect(self, user_client, agreement):
        response = user_client.get(reverse("core:home"), headers={"hx-request": "true"})
        assert response.status_code == 200
        assert response["HX-Redirect"] == reverse("agreements:pending")


class TestPendingView:
    def test_lists_pending_agreements(self, user_client, agreement):
        response = user_client.get(reverse("agreements:pending"))
        assert response.status_code == 200
        assert "Terms of Service" in response.content.decode()

    def test_redirects_home_when_nothing_pending(self, user_client):
        response = user_client.get(reverse("agreements:pending"))
        assert response.status_code == 302
        assert response.url == reverse("core:home")

    def test_requires_login(self, client, agreement):
        response = client.get(reverse("agreements:pending"))
        assert response.status_code == 302
        assert reverse("account_login") in response.url

    def test_accepting_records_the_agreement(self, user_client, user, agreement):
        version_id = agreement.current_version_id
        response = user_client.post(reverse("agreements:pending"), {"accept": [str(version_id)]})
        assert response.status_code == 302
        assert UserAgreement.objects.filter(user=user, agreement_version_id=version_id).exists()

    def test_acceptance_captures_ip_and_user_agent(self, user_client, user, agreement):
        user_client.post(
            reverse("agreements:pending"),
            {"accept": [str(agreement.current_version_id)]},
            REMOTE_ADDR="203.0.113.7",
            headers={"user-agent": "Mozilla/5.0"},
        )
        acceptance = UserAgreement.objects.get(user=user)
        assert acceptance.ip_address == "203.0.113.7"
        assert acceptance.user_agent == "Mozilla/5.0"

    def test_partial_acceptance_records_nothing(self, user_client, user, agreement):
        make_agreement(key="privacy-policy", name="Privacy Policy")
        response = user_client.post(reverse("agreements:pending"), {"accept": [str(agreement.current_version_id)]})
        assert response.status_code == 200
        assert not UserAgreement.objects.filter(user=user).exists()

    def test_accepting_all_records_all(self, user_client, user, agreement):
        privacy = make_agreement(key="privacy-policy", name="Privacy Policy")
        response = user_client.post(
            reverse("agreements:pending"),
            {"accept": [str(agreement.current_version_id), str(privacy.current_version_id)]},
        )
        assert response.status_code == 302
        assert UserAgreement.objects.filter(user=user).count() == 2

    def test_accepting_twice_keeps_one_record(self, user_client, user, agreement):
        version_id = agreement.current_version_id
        user_client.post(reverse("agreements:pending"), {"accept": [str(version_id)]})
        user_client.post(reverse("agreements:pending"), {"accept": [str(version_id)]})
        assert UserAgreement.objects.filter(user=user, agreement_version_id=version_id).count() == 1


class TestHistoryView:
    def test_empty_state(self, user_client):
        response = user_client.get(reverse("agreements:history"))
        assert response.status_code == 200
        assert "not accepted any agreements" in response.content.decode()

    def test_lists_own_acceptances(self, user_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        response = user_client.get(reverse("agreements:history"))
        assert "Terms of Service" in response.content.decode()

    def test_excludes_other_users_acceptances(self, user_client, admin_user, agreement):
        UserAgreement.objects.create(user=admin_user, agreement_version=agreement.current_version)
        response = user_client.get(reverse("agreements:history"))
        assert "not accepted any agreements" in response.content.decode()

    def test_requires_login(self, client, db):
        assert client.get(reverse("agreements:history")).status_code == 302


class TestVersionDetailView:
    def test_renders_markdown_content(self, user_client, agreement):
        response = user_client.get(reverse("agreements:version_detail", args=[agreement.current_version_id]))
        assert response.status_code == 200
        assert "<h1>Terms</h1>" in response.content.decode()

    def test_unknown_version_is_404(self, user_client):
        assert user_client.get(reverse("agreements:version_detail", args=[999999])).status_code == 404

    def test_unpublished_draft_is_not_readable(self, user_client, agreement):
        # Draft terms must not leak before they are announced — IDs here are guessable.
        draft = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="Secret new terms.", effective_date=timezone.now().date()
        )
        assert user_client.get(reverse("agreements:version_detail", args=[draft.pk])).status_code == 404

    def test_superseded_version_stays_readable(self, user_client, agreement):
        first = agreement.current_version
        second = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="v2", effective_date=timezone.now().date()
        )
        second.publish()
        assert user_client.get(reverse("agreements:version_detail", args=[first.pk])).status_code == 200

    def test_shows_acceptance_timestamp(self, user_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        response = user_client.get(reverse("agreements:version_detail", args=[agreement.current_version_id]))
        assert "You accepted this version on" in response.content.decode()


class TestCurrentAgreementView:
    def test_readable_without_logging_in(self, client, agreement):
        response = client.get(reverse("agreements:current", args=[agreement.key]))
        assert response.status_code == 200
        assert "You agree to our terms." in response.content.decode()

    def test_unknown_key_is_404(self, client, db):
        assert client.get(reverse("agreements:current", args=["nope"])).status_code == 404

    def test_unpublished_agreement_is_404(self, client, db):
        agreement = make_agreement(key="draft", name="Draft", publish=False)
        assert client.get(reverse("agreements:current", args=[agreement.key])).status_code == 404


class TestAdmin:
    def test_agreement_change_page_renders_version_inline(self, admin_client, agreement):
        response = admin_client.get(reverse("admin:agreements_agreement_change", args=[agreement.pk]))
        assert response.status_code == 200
        assert "Terms of Service v1" in response.content.decode()

    def test_agreement_history_redirects_to_auditlog(self, admin_client, agreement):
        response = admin_client.get(reverse("admin:agreements_agreement_history", args=[agreement.pk]))
        assert response.status_code == 302
        assert response.url == reverse("admin:agreements_agreement_auditlog", args=[agreement.pk])

    def test_publish_action_sets_current_version(self, admin_client, db):
        agreement = make_agreement(publish=False)
        version = agreement.versions.get()
        admin_client.post(
            reverse("admin:agreements_agreementversion_changelist"),
            {"action": "publish_version", "_selected_action": [str(version.pk)]},
            follow=True,
        )
        agreement.refresh_from_db()
        assert agreement.current_version_id == version.pk

    def test_publish_action_requires_a_single_selection(self, admin_client, db):
        first = make_agreement(publish=False).versions.get()
        second = make_agreement(key="privacy-policy", name="Privacy Policy", publish=False).versions.get()
        admin_client.post(
            reverse("admin:agreements_agreementversion_changelist"),
            {"action": "publish_version", "_selected_action": [str(first.pk), str(second.pk)]},
            follow=True,
        )
        first.refresh_from_db()
        assert not first.is_current

    def test_published_version_is_read_only(self, admin_client, agreement):
        response = admin_client.get(
            reverse("admin:agreements_agreementversion_change", args=[agreement.current_version_id])
        )
        assert response.status_code == 200
        assert "content_markdown" not in response.context["adminform"].form.fields

    def test_version_number_is_hidden_on_the_add_form(self, admin_client, db):
        response = admin_client.get(reverse("admin:agreements_agreementversion_add"))
        assert "version" not in response.context["adminform"].form.fields

    def test_published_version_cannot_be_deleted(self, admin_client, agreement):
        # Deleting the current version would SET_NULL current_version and silently
        # switch the gate off for everyone, with no auditlog entry.
        version = agreement.current_version
        response = admin_client.post(
            reverse("admin:agreements_agreementversion_delete", args=[version.pk]), {"post": "yes"}
        )
        assert response.status_code == 403
        assert AgreementVersion.objects.filter(pk=version.pk).exists()

    def test_unpublished_draft_can_be_deleted(self, admin_client, agreement):
        draft = AgreementVersion.objects.create(
            agreement=agreement, content_markdown="draft", effective_date=timezone.now().date()
        )
        admin_client.post(reverse("admin:agreements_agreementversion_delete", args=[draft.pk]), {"post": "yes"})
        assert not AgreementVersion.objects.filter(pk=draft.pk).exists()

    def test_bulk_delete_action_is_unavailable(self, admin_client, agreement):
        response = admin_client.get(reverse("admin:agreements_agreementversion_changelist"))
        actions = response.context["cl"].model_admin.get_actions(response.wsgi_request)
        assert "delete_selected" not in actions
        assert "publish_version" in actions

    def test_version_change_page_links_to_acceptances(self, admin_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        response = admin_client.get(
            reverse("admin:agreements_agreementversion_change", args=[agreement.current_version_id])
        )
        assert response.status_code == 200
        assert "1 acceptance(s)" in response.content.decode()

    def test_acceptance_link_lookup_is_allowed(self, admin_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        url = reverse("admin:agreements_useragreement_changelist")
        response = admin_client.get(url, {"agreement_version__id__exact": agreement.current_version_id})
        assert response.status_code == 200
        assert response.context["cl"].result_count == 1

    def test_user_agreements_are_read_only(self, admin_client, user, agreement):
        UserAgreement.objects.create(user=user, agreement_version=agreement.current_version)
        response = admin_client.get(reverse("admin:agreements_useragreement_changelist"))
        assert response.status_code == 200
        assert "add" not in response.context["cl"].model_admin.get_actions(response.wsgi_request)


class TestMarkdownFilter:
    def test_renders_headings_and_lists(self):
        html = render_markdown("# Title\n\n- one\n- two")
        assert "<h1>Title</h1>" in html
        assert "<li>one</li>" in html

    def test_empty_input(self):
        assert render_markdown("") == ""

    def test_tables_are_supported(self):
        html = render_markdown("| a | b |\n| - | - |\n| 1 | 2 |")
        assert "<table>" in html

    def test_raw_html_is_escaped(self):
        # The rendered page is public and mark_safe'd, so author HTML must not survive.
        html = render_markdown("Hello <script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_attributes_cannot_break_out(self):
        html = render_markdown('<img src=x onerror="alert(1)">')
        assert "onerror" not in html or "&lt;img" in html
        assert "<img" not in html

    def test_markdown_links_still_render(self):
        html = render_markdown("[terms](https://example.com)")
        assert '<a href="https://example.com">terms</a>' in html
