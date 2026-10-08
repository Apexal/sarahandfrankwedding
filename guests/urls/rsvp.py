"""`/rsvp/` routes. The path of `start` is printed on the invitation card; it cannot move."""

from django.urls import path

from guests.views import rsvp as views

app_name = "rsvp"
urlpatterns = [
    path("", views.start, name="start"),
    path("lookup/", views.lookup, name="lookup"),
    path("select/<int:household_id>/", views.select, name="select"),
    path("respond/", views.respond, name="respond"),
    path("done/", views.done, name="done"),
    path("reset/", views.reset, name="reset"),
]
