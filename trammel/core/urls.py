from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("home/customize/", views.home_customize, name="home_customize"),
    path("setup/", views.setup, name="setup"),
    path("search/", views.search, name="search"),
    path("settings/", views.settings_index, name="settings"),
    path("settings/business/", views.business, name="business"),
    path("settings/rates/", views.rates, name="rates"),
    path("settings/system/", views.system, name="system"),
    path("settings/numbers/", views.sequences, name="sequences"),
    path("settings/numbers/<str:key>/", views.sequence_edit, name="sequence_edit"),
    path("settings/modules/", views.module_list, name="modules"),
    path("settings/modules/<str:key>/", views.module_toggle, name="module_toggle"),
    path("settings/audit/", views.audit_log, name="audit"),
    path("settings/audit.csv", views.audit_csv, name="audit_csv"),
    path("settings/placeholders/", views.template_reference, name="template_reference"),
    path(
        "settings/placeholders/<str:key>/preview/",
        views.template_preview,
        name="template_preview",
    ),
]
