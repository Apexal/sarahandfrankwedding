"""`/save-the-date/` routes. Frozen paths (PLAN.md section 5): the two pages are in sent messages.

`event.ics` and `letter/` are declared before the code route so neither can be read as a code.
The `letter/` routes are the same two pages opened straight to the letter, with no envelope.
"""

from django.urls import path

from guests.views import save_the_date as views

app_name = "save_the_date"
urlpatterns = [
    path("event.ics", views.ics, name="ics"),
    path("", views.generic, name="generic"),
    path("letter/", views.generic, {"letter": True}, name="generic_letter"),
    path("<slug:code>/", views.personalized, name="personalized"),
    path("<slug:code>/letter/", views.personalized, {"letter": True}, name="personalized_letter"),
]
