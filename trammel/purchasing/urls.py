from django.urls import path

from . import views

app_name = "purchasing"

urlpatterns = [
    path("", views.order_list, name="list"),
    path("new/", views.order_new, name="new"),
    path("restock/", views.from_restock, name="from_restock"),
    path("reports/spend/", views.spend_report, name="spend"),
    path("reports/open/", views.open_report, name="open_orders"),
    path("reports/prices/", views.prices_report, name="prices"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/edit/", views.order_edit, name="edit"),
    path("<uuid:pk>/delete/", views.delete, name="delete"),
    path("<uuid:pk>/document/", views.document, name="document"),
    path("<uuid:pk>/lines/new/", views.line_edit, name="line_add"),
    path("<uuid:pk>/lines/<uuid:line_pk>/", views.line_edit, name="line_edit"),
    path("<uuid:pk>/place/", views.place, name="place"),
    path("<uuid:pk>/transition/", views.transition, name="transition"),
    path("<uuid:pk>/receive/", views.receive, name="receive"),
    path("<uuid:pk>/receipts/<uuid:receipt_pk>/undo/", views.unreceive, name="unreceive"),
]
