from django.urls import path

from . import views

app_name = "quotes"

urlpatterns = [
    path("", views.quote_list, name="list"),
    path("templates/", views.template_list, name="templates"),
    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<uuid:pk>/", views.template_edit, name="template_edit"),
    path("templates/<uuid:pk>/archive/", views.template_archive, name="template_archive"),
    path("templates/<uuid:pk>/delete/", views.template_delete, name="template_delete"),
    path("settings/", views.settings_edit, name="settings"),
    path("new/", views.quote_new, name="new"),
    path("new/<uuid:job_pk>/", views.quote_create, name="create"),
    path("r/<uuid:pk>/", views.revision_detail, name="revision"),
    path("r/<uuid:pk>/update/", views.revision_update, name="revision_update"),
    path("r/<uuid:pk>/description/", views.description, name="description"),
    path("r/<uuid:pk>/shipping/", views.shipping, name="shipping"),
    path("r/<uuid:pk>/lines/new/<str:kind>/", views.line_edit, name="line_create"),
    path("r/<uuid:pk>/lines/<uuid:line_pk>/", views.line_edit, name="line_edit"),
    path("r/<uuid:pk>/lines/<uuid:line_pk>/delete/", views.line_delete, name="line_delete"),
    path("r/<uuid:pk>/estimate/<str:key>/", views.estimate, name="estimate"),
    path("r/<uuid:pk>/estimate/<str:key>/<uuid:line_pk>/", views.estimate, name="estimate"),
    path("r/<uuid:pk>/send/", views.send, name="send"),
    path("r/<uuid:pk>/approve/", views.approve, name="approve"),
    path("r/<uuid:pk>/sign/", views.sign, name="sign"),
    path("r/<uuid:pk>/decline/", views.decline, name="decline"),
    path("r/<uuid:pk>/revise/", views.revise, name="revise"),
    path("r/<uuid:pk>/delete/", views.quote_delete, name="delete"),
    path("r/<uuid:pk>/document/", views.document, name="document"),
    path("r/<uuid:pk>/pdf/", views.pdf, name="pdf"),
    path("r/<uuid:pk>/compare/<uuid:other_pk>/", views.compare, name="compare"),
]
