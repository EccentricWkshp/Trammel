from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("", views.settings_page, name="settings"),
    path("paypal/", views.paypal_settings, name="paypal"),
    path("test/", views.test_key, name="test"),
    path("paypal/test/", views.test_paypal, name="test_paypal"),
    path("check/", views.check_now, name="check"),
    path("invoices/<uuid:pk>/link/", views.new_link, name="new_link"),
    path("primary/", views.primary, name="primary"),
    path("invoices/<uuid:pk>/provider/", views.invoice_choice, name="invoice_choice"),
    path("customers/<uuid:pk>/provider/", views.customer_choice, name="customer_choice"),
]
