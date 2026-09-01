import posthog
from allauth.account.signals import (
    password_changed,
    password_reset,
    password_set,
    user_logged_in,
    user_logged_out,
    user_signed_up,
)
from allauth.mfa.signals import authenticator_added, authenticator_removed, authenticator_reset


def on_user_signed_up(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("user_signed_up", properties={"email": user.email})
    posthog.set(distinct_id=str(user.pk), properties={"email": user.email, "name": user.name or ""})


def on_user_logged_in(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("user_logged_in")


def on_user_logged_out(request, user, **kwargs):
    if user:
        posthog.identify_context(str(user.pk))
        posthog.capture("user_logged_out")


def on_password_changed(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("password_changed")


def on_password_reset(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("password_reset")


def on_password_set(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("password_set")


def on_authenticator_added(request, user, authenticator, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("mfa_added", properties={"method": authenticator.type})


def on_authenticator_removed(request, user, authenticator, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("mfa_removed", properties={"method": authenticator.type})


def on_authenticator_reset(request, user, **kwargs):
    posthog.identify_context(str(user.pk))
    posthog.capture("mfa_reset")


user_signed_up.connect(on_user_signed_up)
user_logged_in.connect(on_user_logged_in)
user_logged_out.connect(on_user_logged_out)
password_changed.connect(on_password_changed)
password_reset.connect(on_password_reset)
password_set.connect(on_password_set)
authenticator_added.connect(on_authenticator_added)
authenticator_removed.connect(on_authenticator_removed)
authenticator_reset.connect(on_authenticator_reset)
