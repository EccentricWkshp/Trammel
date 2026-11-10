from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.product_list, name="list"),
    path("export.csv", views.product_csv, name="csv"),
    path("new/", views.product_edit, name="create"),
    path("<uuid:pk>/", views.product_edit, name="edit"),
    path("<uuid:pk>/archive/", views.product_archive, name="archive"),
    path("<uuid:pk>/delete/", views.product_delete, name="delete"),
]
