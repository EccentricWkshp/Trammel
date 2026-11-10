from django.urls import path

from . import views

app_name = "stock"

urlpatterns = [
    path("", views.stock_list, name="list"),
    path("new/", views.add_material, name="add"),
    path("track/<uuid:pk>/", views.track, name="track"),
    path("valuation.csv", views.valuation_csv, name="valuation_csv"),
    path("restock/", views.restock, name="restock"),
    path("moves/", views.moves, name="moves"),
    path("settings/", views.settings_view, name="settings"),
    path("locations/", views.location_list, name="locations"),
    path("locations/new/", views.location_edit, name="location_create"),
    path("locations/<uuid:pk>/", views.location_detail, name="location"),
    path("locations/<uuid:pk>/edit/", views.location_edit, name="location_edit"),
    path("lots/<uuid:pk>/", views.lot_detail, name="lot"),
    path("lots/<uuid:pk>/<str:action>/", views.lot_action, name="lot_action"),
    path("materials/<uuid:pk>/tracking/", views.tracking, name="tracking"),
    path("materials/<uuid:pk>/receive/", views.receive, name="receive"),
    path("materials/<uuid:pk>/count/", views.count, name="count"),
]
