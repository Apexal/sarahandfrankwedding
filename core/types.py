from django.http import HttpRequest
from django_htmx.middleware import HtmxDetails

from core.models import User


class AppRequest(HttpRequest):
    htmx: HtmxDetails
    user: User
