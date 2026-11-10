from django.urls import path

from . import views

app_name = "workorders"

urlpatterns = [
    path("", views.wo_list, name="list"),
    path("new/", views.wo_new, name="new"),
    path("new/<uuid:job_pk>/", views.wo_create, name="create"),
    path("<uuid:pk>/", views.wo_detail, name="detail"),
    path("<uuid:pk>/edit/", views.wo_edit, name="edit"),
    path("<uuid:pk>/transition/", views.wo_transition, name="transition"),
    path("<uuid:pk>/operations/new/", views.op_add, name="op_add"),
    path("<uuid:pk>/operations/<uuid:op_pk>/", views.op_set, name="op_set"),
    path("<uuid:pk>/operations/<uuid:op_pk>/edit/", views.op_edit, name="op_edit"),
    path("<uuid:pk>/operations/<uuid:op_pk>/move/", views.op_move, name="op_move"),
    path("<uuid:pk>/operations/<uuid:op_pk>/delete/", views.op_delete, name="op_delete"),
    path("<uuid:pk>/notes/", views.note_add, name="note_add"),
    path("<uuid:pk>/materials/", views.material_add, name="material_add"),
    path("<uuid:pk>/materials/confirm/", views.materials_confirm, name="materials_confirm"),
    path("<uuid:pk>/traveler/", views.traveler, name="traveler"),
    path("change-orders/new/<uuid:job_pk>/", views.co_create, name="change_order_create"),
    path("change-orders/<uuid:pk>/", views.co_detail, name="change_order"),
    path("change-orders/<uuid:pk>/action/", views.co_action, name="change_order_action"),
    path(
        "change-orders/<uuid:pk>/lines/<uuid:line_pk>/delete/",
        views.co_line_delete,
        name="change_order_line_delete",
    ),
]
