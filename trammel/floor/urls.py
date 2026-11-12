from django.urls import path

from . import views

app_name = "floor"

urlpatterns = [
    path("", views.home, name="home"),
    path("find/", views.find, name="find"),
    path("scan/", views.scan, name="scan"),
    path("schedule/", views.schedule, name="schedule"),
    path("wo/<uuid:pk>/", views.work_order, name="work_order"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
]
