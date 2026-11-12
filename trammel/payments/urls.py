from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("test/", views.test_key, name="test"),
    path("check/", views.check_now, name="check"),
    path("invoices/<uuid:pk>/link/", views.new_link, name="new_link"),
]
