from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("profitability/", views.profitability, name="profitability"),
    path("sales/", views.sales, name="sales"),
    path("aging/", views.aging, name="aging"),
    path("tax/", views.tax, name="tax"),
    path("time/", views.time, name="time"),
    path("force-closed/", views.force_closed, name="force_closed"),
]
