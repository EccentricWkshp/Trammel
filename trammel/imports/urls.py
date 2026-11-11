from django.urls import path

from . import views

app_name = "imports"

urlpatterns = [
    path("", views.order_list, name="list"),
    path("fetch/", views.fetch, name="fetch"),
    path("<uuid:pk>/complete/", views.complete, name="complete"),
    path("<uuid:pk>/discard/", views.discard, name="discard"),
    path("<uuid:pk>/job/", views.make_job, name="make_job"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/test/", views.test_connection, name="test"),
    path("shopify/webhook/", views.shopify_webhook, name="shopify_webhook"),
]
