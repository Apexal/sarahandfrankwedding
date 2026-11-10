from django.urls import path

from guests.views import guest_list, manage, seating

app_name = "manage"
urlpatterns = [
    path("", manage.dashboard, name="dashboard"),
    path("guests/", guest_list.guest_list_page, name="guest_list"),
    path("guests/totals/", guest_list.totals, name="guest_list_totals"),
    path("guests/add/", guest_list.household_add, name="household_add"),
    path("guests/reorder/", guest_list.households_reorder, name="households_reorder"),
    path("guests/household/<int:pk>/", guest_list.household_card, name="household_card"),
    path("guests/household/<int:pk>/edit/", guest_list.household_edit, name="household_edit"),
    path("guests/household/<int:pk>/delete/", guest_list.household_delete, name="household_delete"),
    path("guests/household/<int:pk>/plus-one/", guest_list.household_plus_one, name="household_plus_one"),
    path("guests/household/<int:pk>/events/<int:event_pk>/", guest_list.household_event, name="household_event"),
    path("guests/household/<int:pk>/guests/add/", guest_list.guest_add, name="guest_add"),
    path("guests/household/<int:pk>/guests/reorder/", guest_list.guests_reorder, name="guests_reorder"),
    path("guests/guest/<int:pk>/edit/", guest_list.guest_edit, name="guest_edit"),
    path("guests/guest/<int:pk>/delete/", guest_list.guest_delete, name="guest_delete"),
    path("guests/guest/<int:pk>/move/", guest_list.guest_move, name="guest_move"),
    path("seating/", seating.seating, name="seating"),
    path("seating/print/", seating.seating_print, name="seating_print"),
    path("export/rsvps.csv", manage.export_rsvps, name="export_rsvps"),
    path("export/seating.csv", manage.export_seating, name="export_seating"),
    path("export/save-the-date-links.csv", manage.export_save_the_date_links, name="export_save_the_date_links"),
    path("export/invite-codes.csv", manage.export_invite_codes, name="export_invite_codes"),
    path("qr/<slug:name>.svg", manage.qr, name="qr"),
]
