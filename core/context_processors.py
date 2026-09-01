from django.conf import settings
from django.http import HttpRequest

from core.constants import COMPANY, SESSION_IMPERSONATOR_USER_ID_KEY
from core.models import AppSettings, User


def global_context(request: HttpRequest):
    impersonator = None
    if hasattr(request, "session"):
        impersonator_id = request.session.get(SESSION_IMPERSONATOR_USER_ID_KEY)
        if impersonator_id:
            impersonator = User.objects.filter(pk=impersonator_id).values("first_name", "last_name", "email").first()

    return {
        "debug": settings.DEBUG,
        "production": settings.ENV == "production",
        "environment": settings.ENV,
        "company": COMPANY,
        "app_version": settings.APP_VERSION,
        "app_settings": AppSettings.load(),
        "impersonator": impersonator,
    }
