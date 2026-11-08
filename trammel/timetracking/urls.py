from django.urls import path

from . import views

app_name = "time"

urlpatterns = [
    path("", views.entry_list, name="list"),
    path("export.csv", views.entry_csv, name="csv"),
    path("start/<uuid:wo_pk>/", views.start, name="start"),
    path("add/<uuid:wo_pk>/", views.manual, name="manual"),
    path("<uuid:pk>/stop/", views.stop, name="stop"),
    path("<uuid:pk>/correct/", views.correct, name="correct"),
]
