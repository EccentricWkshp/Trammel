from django.urls import path

from . import views

app_name = "edge"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("deploy/", views.start_deploy, name="deploy"),
    path("sync/", views.sync_now, name="sync"),
    path("stop/", views.stop, name="stop"),
]
