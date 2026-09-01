import re
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.test import Client
from django.utils import timezone

from conftest import TEST_PASSWORD, set_registration
from core.models import AccessToken, AccountDeletionRequest, EmailVerificationCode, RefreshToken, User
from core.tasks import GRACE_PERIOD, _sweep_due_account_deletions
from messaging.models import OutboundEmail

pytestmark = pytest.mark.django_db


def post(client: Client, path: str, payload: dict, *, token: str | None = None):
    headers = {"AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(f"/api{path}", payload, content_type="application/json", headers=headers)


def patch(client: Client, path: str, payload: dict, *, token: str | None = None):
    headers = {"AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.patch(f"/api{path}", payload, content_type="application/json", headers=headers)


def get(client: Client, path: str, *, token: str | None = None):
    headers = {"AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.get(f"/api{path}", headers=headers)


def latest_code_for(email: str) -> str:
    """Pull the OTP out of the rendered email — it is only ever hashed in the DB."""
    outbound = OutboundEmail.objects.filter(to_email=email, template_key="verification_code").latest("created_at")
    match = re.search(r"\b(\d{6})\b", outbound.body_text_rendered)
    assert match, f"no 6-digit code in rendered email: {outbound.body_text_rendered!r}"
    return match.group(1)


@pytest.fixture
def tokens(client, user):
    response = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD})
    assert response.status_code == 200
    return response.json()


@pytest.mark.usefixtures("open_registration")
class TestRegistration:
    def test_register_creates_inactive_user_and_sends_code(self, client):
        response = post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})

        assert response.status_code == 200
        created = User.objects.get(email="new@example.com")
        assert created.is_active is False
        assert created.username, "allauth's adapter should have generated a username"
        assert EmailAddress.objects.filter(user=created, verified=False).exists()
        assert latest_code_for("new@example.com")

    def test_register_is_closed_when_the_setting_is_off(self, client):
        set_registration(allowed=False)

        response = post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})

        assert response.status_code == 403
        assert not User.objects.filter(email="new@example.com").exists()

    def test_register_rejects_weak_passwords(self, client):
        response = post(client, "/auth/register", {"email": "new@example.com", "password": "pw"})

        assert response.status_code == 422
        assert not User.objects.filter(email="new@example.com").exists()

    def test_register_does_not_leak_existing_verified_accounts(self, client, user):
        response = post(client, "/auth/register", {"email": user.email, "password": "sufficiently-long-pw"})

        # Same neutral body as a fresh signup, and no email sent to the existing account.
        assert response.status_code == 200
        assert "verification code" in response.json()["detail"]
        assert not OutboundEmail.objects.filter(to_email=user.email).exists()

    def test_register_reissues_a_code_for_an_unverified_account(self, client):
        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})
        first_code = latest_code_for("new@example.com")

        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})

        assert User.objects.filter(email="new@example.com").count() == 1
        assert latest_code_for("new@example.com") != first_code

    def test_verify_email_activates_the_account_and_returns_tokens(self, client):
        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})
        code = latest_code_for("new@example.com")

        response = post(client, "/auth/verify-email", {"email": "new@example.com", "code": code})

        assert response.status_code == 200
        assert set(response.json()) == {"access_token", "refresh_token"}
        created = User.objects.get(email="new@example.com")
        assert created.is_active is True
        # Verified on the web side too, so the same person can sign in at /accounts/login/.
        assert EmailAddress.objects.get(user=created).verified is True

    def test_verify_email_rejects_a_wrong_code_and_counts_the_attempt(self, client):
        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})

        response = post(client, "/auth/verify-email", {"email": "new@example.com", "code": "000000"})

        assert response.status_code == 422
        assert EmailVerificationCode.objects.get(user__email="new@example.com").attempts == 1

    def test_verify_email_rejects_an_expired_code(self, client):
        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})
        code = latest_code_for("new@example.com")
        EmailVerificationCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

        response = post(client, "/auth/verify-email", {"email": "new@example.com", "code": code})

        assert response.status_code == 422
        assert User.objects.get(email="new@example.com").is_active is False

    def test_a_code_cannot_be_redeemed_twice(self, client):
        post(client, "/auth/register", {"email": "new@example.com", "password": "sufficiently-long-pw"})
        code = latest_code_for("new@example.com")
        post(client, "/auth/verify-email", {"email": "new@example.com", "code": code})

        response = post(client, "/auth/verify-email", {"email": "new@example.com", "code": code})

        assert response.status_code == 422

    def test_resend_verification_is_neutral_for_unknown_addresses(self, client):
        response = post(client, "/auth/resend-verification", {"email": "nobody@example.com"})

        assert response.status_code == 200
        assert not OutboundEmail.objects.exists()


class TestLogin:
    def test_valid_credentials_return_a_token_pair(self, client, user):
        response = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD})

        assert response.status_code == 200
        assert set(response.json()) == {"access_token", "refresh_token"}

    def test_login_is_case_insensitive_on_email(self, client, user):
        response = post(client, "/auth/token", {"email": user.email.upper(), "password": TEST_PASSWORD})

        assert response.status_code == 200

    def test_wrong_password_is_rejected(self, client, user):
        response = post(client, "/auth/token", {"email": user.email, "password": "nope"})

        assert response.status_code == 401

    def test_inactive_users_cannot_log_in(self, client, user):
        user.is_active = False
        user.save()

        response = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD})

        assert response.status_code == 401

    def test_access_and_refresh_tokens_share_a_family(self, client, user):
        post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD})

        assert AccessToken.objects.get(user=user).family == RefreshToken.objects.get(user=user).family


class TestTokenAuth:
    def test_me_requires_a_token(self, client):
        assert get(client, "/me").status_code == 401

    def test_me_rejects_a_garbage_token(self, client):
        assert get(client, "/me", token="not-a-real-token").status_code == 401

    def test_me_returns_the_authenticated_user(self, client, user, tokens):
        response = get(client, "/me", token=tokens["access_token"])

        assert response.status_code == 200
        assert response.json()["email"] == user.email

    def test_a_revoked_token_stops_working(self, client, tokens):
        AccessToken.objects.update(revoked_at=timezone.now())

        assert get(client, "/me", token=tokens["access_token"]).status_code == 401

    def test_an_expired_token_stops_working(self, client, tokens):
        AccessToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        assert get(client, "/me", token=tokens["access_token"]).status_code == 401

    def test_deactivating_a_user_invalidates_their_tokens(self, client, user, tokens):
        user.is_active = False
        user.save()

        assert get(client, "/me", token=tokens["access_token"]).status_code == 401


class TestRefresh:
    def test_refresh_returns_a_new_pair_and_rotates_the_old_token(self, client, tokens):
        response = post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]})

        assert response.status_code == 200
        assert response.json()["refresh_token"] != tokens["refresh_token"]
        assert RefreshToken.find(tokens["refresh_token"]).revoked_at is not None

    def test_the_new_access_token_works(self, client, tokens):
        refreshed = post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]}).json()

        assert get(client, "/me", token=refreshed["access_token"]).status_code == 200

    def test_rotation_stays_in_the_original_family(self, client, tokens):
        original_family = RefreshToken.find(tokens["refresh_token"]).family

        refreshed = post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]}).json()

        assert RefreshToken.find(refreshed["refresh_token"]).family == original_family

    def test_replaying_a_rotated_token_revokes_the_whole_family(self, client, tokens):
        refreshed = post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]}).json()

        # The attacker replays the token the legitimate client already spent.
        response = post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]})

        assert response.status_code == 401
        # Both parties are now locked out — the replacement pair is dead too.
        assert RefreshToken.find(refreshed["refresh_token"]).revoked_at is not None
        assert get(client, "/me", token=refreshed["access_token"]).status_code == 401

    def test_an_unknown_refresh_token_is_rejected(self, client):
        assert post(client, "/auth/refresh", {"refresh_token": "made-up"}).status_code == 401


class TestLogout:
    def test_logout_revokes_both_tokens(self, client, tokens):
        response = post(
            client, "/auth/logout", {"refresh_token": tokens["refresh_token"]}, token=tokens["access_token"]
        )

        assert response.status_code == 200
        assert get(client, "/me", token=tokens["access_token"]).status_code == 401
        assert post(client, "/auth/refresh", {"refresh_token": tokens["refresh_token"]}).status_code == 401

    def test_logout_requires_authentication(self, client, tokens):
        assert post(client, "/auth/logout", {"refresh_token": tokens["refresh_token"]}).status_code == 401


class TestProfile:
    def test_update_name(self, client, user, tokens):
        response = patch(client, "/me", {"first_name": " Ada ", "last_name": "Lovelace"}, token=tokens["access_token"])

        assert response.status_code == 200
        assert response.json()["first_name"] == "Ada"
        user.refresh_from_db()
        assert user.last_name == "Lovelace"

    def test_update_phone_normalizes_to_e164(self, client, tokens):
        response = patch(client, "/me/phone", {"phone_number": "(555) 123-4567"}, token=tokens["access_token"])

        assert response.status_code == 200
        assert response.json()["phone_number"] == "+15551234567"

    def test_update_phone_rejects_nonsense(self, client, tokens):
        assert patch(client, "/me/phone", {"phone_number": "123"}, token=tokens["access_token"]).status_code == 422

    def test_phone_can_be_cleared(self, client, tokens):
        patch(client, "/me/phone", {"phone_number": "5551234567"}, token=tokens["access_token"])

        response = patch(client, "/me/phone", {"phone_number": ""}, token=tokens["access_token"])

        assert response.json()["phone_number"] == ""

    def test_change_password_requires_the_current_one(self, client, tokens):
        response = post(
            client,
            "/me/password",
            {"current_password": "wrong", "new_password": "a-brand-new-password"},
            token=tokens["access_token"],
        )

        assert response.status_code == 422

    def test_change_password_rejects_weak_passwords(self, client, tokens):
        response = post(
            client,
            "/me/password",
            {"current_password": TEST_PASSWORD, "new_password": "pw"},
            token=tokens["access_token"],
        )

        assert response.status_code == 422

    def test_change_password_signs_out_other_devices(self, client, user, tokens):
        other_device = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD}).json()

        response = post(
            client,
            "/me/password",
            {"current_password": TEST_PASSWORD, "new_password": "a-brand-new-password"},
            token=tokens["access_token"],
        )

        assert response.status_code == 200
        # The device that changed the password stays signed in; the other does not.
        assert get(client, "/me", token=tokens["access_token"]).status_code == 200
        assert get(client, "/me", token=other_device["access_token"]).status_code == 401


class TestAccountDeletion:
    def test_requesting_deletion_schedules_it_and_revokes_tokens(self, client, user, tokens):
        response = post(client, "/account/delete", {"reason": "moving on"}, token=tokens["access_token"])

        assert response.status_code == 200
        assert response.json()["is_cancelable"] is True
        assert AccountDeletionRequest.objects.get(user=user).status == AccountDeletionRequest.Status.SCHEDULED
        # Tokens are dead, but the account is not — you must log in again to cancel.
        assert get(client, "/me", token=tokens["access_token"]).status_code == 401
        user.refresh_from_db()
        assert user.is_active is True

    def test_requesting_twice_returns_the_same_request(self, client, user, tokens):
        first = post(client, "/account/delete", {}, token=tokens["access_token"]).json()
        fresh = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD}).json()

        second = post(client, "/account/delete", {}, token=fresh["access_token"]).json()

        assert first["id"] == second["id"]
        assert AccountDeletionRequest.objects.count() == 1

    def test_cancel_restores_the_account(self, client, user, tokens):
        post(client, "/account/delete", {}, token=tokens["access_token"])
        fresh = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD}).json()

        response = post(client, "/account/delete/cancel", {}, token=fresh["access_token"])

        assert response.status_code == 200
        assert AccountDeletionRequest.objects.get(user=user).status == AccountDeletionRequest.Status.CANCELED
        assert get(client, "/account/delete", token=fresh["access_token"]).json() is None

    def test_cancel_with_no_pending_request_is_a_404(self, client, tokens):
        assert post(client, "/account/delete/cancel", {}, token=tokens["access_token"]).status_code == 404

    def test_cancel_after_the_grace_period_is_a_409(self, client, user, tokens):
        post(client, "/account/delete", {}, token=tokens["access_token"])
        AccountDeletionRequest.objects.update(scheduled_for=timezone.now() - timedelta(seconds=1))
        fresh = post(client, "/auth/token", {"email": user.email, "password": TEST_PASSWORD}).json()

        assert post(client, "/account/delete/cancel", {}, token=fresh["access_token"]).status_code == 409

    def test_the_sweep_ignores_requests_still_in_their_grace_period(self, client, tokens):
        post(client, "/account/delete", {}, token=tokens["access_token"])

        assert _sweep_due_account_deletions() == 0

    def test_the_sweep_picks_up_requests_past_due(self, client, tokens):
        post(client, "/account/delete", {}, token=tokens["access_token"])
        AccountDeletionRequest.objects.update(scheduled_for=timezone.now() - timedelta(seconds=1))

        assert _sweep_due_account_deletions() == 1

    def test_executing_a_deletion_removes_the_user_but_keeps_the_record(self, client, user, tokens):
        from core.deletion import execute_account_deletion

        post(client, "/account/delete", {}, token=tokens["access_token"])
        deletion_request = AccountDeletionRequest.objects.get(user=user)

        execute_account_deletion(user, deletion_request=deletion_request)

        assert not User.objects.filter(pk=user.pk).exists()
        deletion_request.refresh_from_db()
        assert deletion_request.status == AccountDeletionRequest.Status.COMPLETED
        assert deletion_request.user is None  # SET_NULL — the record outlives the account
        assert deletion_request.completed_at is not None

    def test_deletion_handlers_run_before_the_user_is_deleted(self, client, user, tokens, monkeypatch):
        from core import deletion

        seen = []
        monkeypatch.setattr(deletion, "DELETION_HANDLERS", [lambda u: seen.append(u.email)])
        post(client, "/account/delete", {}, token=tokens["access_token"])

        deletion.execute_account_deletion(user, deletion_request=AccountDeletionRequest.objects.get(user=user))

        assert seen == [user.email]

    def test_grace_period_is_applied_to_the_schedule(self, client, tokens):
        before = timezone.now()

        post(client, "/account/delete", {}, token=tokens["access_token"])

        scheduled_for = AccountDeletionRequest.objects.get().scheduled_for
        assert before + GRACE_PERIOD <= scheduled_for <= timezone.now() + GRACE_PERIOD
