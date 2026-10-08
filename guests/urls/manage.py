from django.urls import path

from guests.views import manage, seating

app_name = "manage"
urlpatterns = [
    path("", manage.dashboard, name="dashboard"),
    path("seating/", seating.seating, name="seating"),
    path("seating/print/", seating.seating_print, name="seating_print"),
    path("export/rsvps.csv", manage.export_rsvps, name="export_rsvps"),
    path("export/seating.csv", manage.export_seating, name="export_seating"),
    path("export/save-the-date-links.csv", manage.export_save_the_date_links, name="export_save_the_date_links"),
    path("export/invite-codes.csv", manage.export_invite_codes, name="export_invite_codes"),
    path("qr/<slug:name>.svg", manage.qr, name="qr"),
]
