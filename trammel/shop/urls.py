from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("machines/", views.machine_list, name="machines"),
    path("machines/new/", views.edit, {"kind": "machine"}, name="machine_create"),
    path("machines/<uuid:pk>/", views.edit, {"kind": "machine"}, name="machine_edit"),
    path("materials/", views.material_list, name="materials"),
    path("materials/new/", views.edit, {"kind": "material"}, name="material_create"),
    path("materials/<uuid:pk>/", views.edit, {"kind": "material"}, name="material_edit"),
    path("vendors/", views.vendor_list, name="vendors"),
    path("vendors/new/", views.edit, {"kind": "vendor"}, name="vendor_create"),
    path("vendors/<uuid:pk>/", views.edit, {"kind": "vendor"}, name="vendor_edit"),
]
