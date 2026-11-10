from django.urls import path

from . import floor, views

app_name = "stock"

urlpatterns = [
    path("", views.stock_list, name="list"),
    path("new/", views.add_material, name="add"),
    path("track/<uuid:pk>/", views.track, name="track"),
    path("valuation.csv", views.valuation_csv, name="valuation_csv"),
    path("labels/", views.label_options, name="labels"),
    path("labels/print/", views.label_print, name="label_print"),
    path("f/", floor.index, name="floor_index"),
    path("f/lot/<uuid:pk>/", floor.lot, name="floor_lot"),
    path("f/location/<uuid:pk>/", floor.location, name="floor_location"),
    path("f/material/<uuid:pk>/", floor.material, name="floor_material"),
    path("f/use/", floor.use, name="floor_use"),
    path("restock/", views.restock, name="restock"),
    path("moves/", views.moves, name="moves"),
    path("remnants/", views.remnant_list, name="remnants"),
    path("reports/valuation/", views.valuation_report, name="valuation"),
    path("reports/adjustments/", views.adjustments_report, name="adjustments"),
    path("settings/", views.settings_view, name="settings"),
    path("locations/", views.location_list, name="locations"),
    path("locations/new/", views.location_edit, name="location_create"),
    path("locations/<uuid:pk>/", views.location_detail, name="location"),
    path("locations/<uuid:pk>/edit/", views.location_edit, name="location_edit"),
    path("locations/<uuid:pk>/archive/", views.location_archive, name="location_archive"),
    path("locations/<uuid:pk>/delete/", views.location_delete, name="location_delete"),
    path("lots/<uuid:pk>/", views.lot_detail, name="lot"),
    path("lots/<uuid:pk>/remnant/", views.cut_remnant, name="cut_remnant"),
    path("lots/<uuid:pk>/<str:action>/", views.lot_action, name="lot_action"),
    path("materials/<uuid:pk>/tracking/", views.tracking, name="tracking"),
    path("products/<uuid:product_pk>/bom/", views.bom_add, name="bom_add"),
    path("products/<uuid:product_pk>/cost/", views.bom_cost, name="bom_cost"),
    path("bom/<uuid:pk>/", views.bom_edit, name="bom_edit"),
    path("bom/<uuid:pk>/delete/", views.bom_delete, name="bom_delete"),
    path("materials/<uuid:pk>/receive/", views.receive, name="receive"),
    path("materials/<uuid:pk>/count/", views.count, name="count"),
]
