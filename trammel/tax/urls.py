from django.urls import path

from . import views

app_name = "tax"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("settings/", views.settings_edit, name="settings"),
    path("jurisdictions/new/", views.jurisdiction_edit, name="jurisdiction_create"),
    path("jurisdictions/<uuid:pk>/", views.jurisdiction_edit, name="jurisdiction_edit"),
    path("rates/new/", views.rate_create, name="rate_create"),
    path("rates/<uuid:pk>/end/", views.rate_end, name="rate_end"),
    path("rates/remove/", views.rates_remove, name="rates_remove"),
    path("boundaries/new/", views.boundary_create, name="boundary_create"),
    path("categories/new/", views.category_edit, name="category_create"),
    path("categories/<uuid:pk>/", views.category_edit, name="category_edit"),
    path("import/", views.import_file, name="import"),
    path("imports/<uuid:pk>/remove/", views.import_remove, name="import_remove"),
]
