from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.smtp, name="smtp"),
    path("notifications/", views.notification_settings, name="settings"),
    path("templates/<str:event>/", views.template_edit, name="template"),
    path("templates/<str:event>/toggle/", views.template_toggle, name="template_toggle"),
    path("log/", views.email_log, name="log"),
    path("quote/<uuid:pk>/", views.email_quote, name="email_quote"),
    path("invoice/<uuid:pk>/", views.email_invoice, name="email_invoice"),
]
