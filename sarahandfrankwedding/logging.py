import structlog
from django.dispatch import receiver
from django_structlog import signals


@receiver(signals.bind_extra_request_metadata)
def bind_domain(request, logger, **kwargs):
    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        structlog.contextvars.bind_contextvars(
            username=user.username,
            email=user.email,
            is_staff=user.is_staff or user.is_superuser,
        )
