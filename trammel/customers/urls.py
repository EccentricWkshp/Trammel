from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("export.csv", views.customer_csv, name="csv"),
    path("new/", views.customer_create, name="create"),
    path("<uuid:pk>/", views.customer_detail, name="detail"),
    path("<uuid:pk>/edit/", views.customer_edit, name="edit"),
    path("<uuid:pk>/delete/", views.customer_delete, name="delete"),
    path("<uuid:pk>/<str:kind>/new/", views.child_edit, name="child_create"),
    path("<uuid:pk>/<str:kind>/<uuid:child_pk>/", views.child_edit, name="child_edit"),
    path("<uuid:pk>/<str:kind>/<uuid:child_pk>/delete/", views.child_delete, name="child_delete"),
]
