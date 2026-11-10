from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("machines/", views.machine_list, name="machines"),
    path("machines/new/", views.edit, {"kind": "machine"}, name="machine_create"),
    path("machines/<uuid:pk>/", views.edit, {"kind": "machine"}, name="machine_edit"),
    path("materials/", views.material_list, name="materials"),
    path("materials/new/", views.edit, {"kind": "material"}, name="material_create"),
    path("materials/<uuid:pk>/", views.material_detail, name="material"),
    path("materials/<uuid:pk>/edit/", views.edit, {"kind": "material"}, name="material_edit"),
    path("materials/<uuid:pk>/conversions/", views.conversion_add, name="conversion_add"),
    path(
        "materials/<uuid:pk>/conversions/<uuid:row_pk>/delete/",
        views.conversion_delete,
        name="conversion_delete",
    ),
    path("materials/<uuid:pk>/barcodes/", views.barcode_add, name="barcode_add"),
    path(
        "materials/<uuid:pk>/barcodes/<uuid:row_pk>/delete/",
        views.barcode_delete,
        name="barcode_delete",
    ),
    path("vendors/", views.vendor_list, name="vendors"),
    path("vendors/new/", views.edit, {"kind": "vendor"}, name="vendor_create"),
    path("vendors/<uuid:pk>/", views.edit, {"kind": "vendor"}, name="vendor_edit"),
]
