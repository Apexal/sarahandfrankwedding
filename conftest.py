import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from core.models import User

TEST_PASSWORD = "testpass123"


@pytest.fixture(autouse=True)
def _isolate_cache():
    # LocMemCache outlives the per-test transaction, so cached state (e.g. the
    # WeddingSettings singleton read by SitePasswordMiddleware) would otherwise
    # leak between tests.
    cache.clear()


def _create_verified_user(*, is_superuser=False, **kwargs):
    """Create a user with the verified EmailAddress row allauth requires for login.

    The plaintext password is stashed on `.raw_password` for tests that log in
    through the real login form.
    """
    kwargs.setdefault("password", TEST_PASSWORD)
    create = User.objects.create_superuser if is_superuser else User.objects.create_user
    created_user = create(**kwargs)
    EmailAddress.objects.create(user=created_user, email=created_user.email, verified=True, primary=True)
    created_user.raw_password = kwargs["password"]
    return created_user


@pytest.fixture
def user(db):
    return _create_verified_user(
        username="testuser",
        email="testuser@example.com",
        first_name="Test",
        last_name="User",
    )


@pytest.fixture
def staff_user(db):
    return _create_verified_user(username="staffer", email="staffer@example.com", is_staff=True)


@pytest.fixture
def admin_user(db):
    """Superuser. Overrides pytest-django's fixture, so `admin_client` logs in as this user."""
    return _create_verified_user(
        is_superuser=True,
        username="admin",
        email="admin@example.com",
        first_name="Admin",
        last_name="User",
    )


@pytest.fixture
def user_client(user):
    """Client logged in as a regular verified user — the non-staff sibling of `admin_client`."""
    client = Client()
    client.force_login(user)
    return client
