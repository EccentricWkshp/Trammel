from django.urls import path

from . import views

app_name = "jobs"

urlpatterns = [
    path("", views.job_list, name="list"),
    path("export.csv", views.job_csv, name="csv"),
    path("calendar/", views.job_calendar, name="calendar"),
    path("calendar/default/", views.calendar_default, name="calendar_default"),
    path("new/", views.job_create, name="create"),
    path("<uuid:pk>/", views.job_detail, name="detail"),
    path("<uuid:pk>/edit/", views.job_edit, name="edit"),
    path("<uuid:pk>/transition/", views.job_transition, name="transition"),
    path("<uuid:pk>/force-close/", views.job_force_close, name="force_close"),
    path("<uuid:pk>/duplicate/", views.job_duplicate, name="duplicate"),
    path("<uuid:pk>/delete/", views.job_delete, name="delete"),
]
