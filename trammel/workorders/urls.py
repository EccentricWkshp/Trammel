from django.urls import path

from . import schedule_views, views

app_name = "workorders"

urlpatterns = [
    path("", views.wo_list, name="list"),
    path("schedule/", schedule_views.schedule, name="schedule"),
    path("new/", views.wo_new, name="new"),
    path("new/<uuid:job_pk>/", views.wo_create, name="create"),
    path("<uuid:pk>/", views.wo_detail, name="detail"),
    path("<uuid:pk>/edit/", views.wo_edit, name="edit"),
    path("<uuid:pk>/transition/", views.wo_transition, name="transition"),
    path("<uuid:pk>/queue/", views.queue_move, name="queue_move"),
    path("<uuid:pk>/operations/new/", views.op_add, name="op_add"),
    path("<uuid:pk>/operations/<uuid:op_pk>/", views.op_set, name="op_set"),
    path("<uuid:pk>/operations/<uuid:op_pk>/edit/", views.op_edit, name="op_edit"),
    path("<uuid:pk>/operations/<uuid:op_pk>/move/", views.op_move, name="op_move"),
    path("<uuid:pk>/operations/<uuid:op_pk>/delete/", views.op_delete, name="op_delete"),
    path("<uuid:pk>/items/<uuid:item_pk>/", views.item_set, name="item_set"),
    path("<uuid:pk>/items/<uuid:item_pk>/delete/", views.item_delete, name="item_delete"),
    path("<uuid:pk>/notes/", views.note_add, name="note_add"),
    path("<uuid:pk>/notes/<uuid:note_pk>/share/", views.note_share, name="note_share"),
    path("<uuid:pk>/materials/", views.material_add, name="material_add"),
    path("<uuid:pk>/materials/confirm/", views.materials_confirm, name="materials_confirm"),
    path("<uuid:pk>/materials/planned/", views.planned_use, name="planned_use"),
    path("<uuid:pk>/traveler/", views.traveler, name="traveler"),
    path(
        "<uuid:pk>/change-orders/",
        views.add_change_order,
        name="add_change_order",
    ),
]
