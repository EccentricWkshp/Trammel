from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("", views.invoice_list, name="list"),
    path("export.csv", views.invoice_csv, name="csv"),
    path("new/", views.invoice_create, name="create"),
    path("templates/", views.template_list, name="templates"),
    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<uuid:pk>/", views.template_edit, name="template_edit"),
    path("templates/<uuid:pk>/archive/", views.template_archive, name="template_archive"),
    path("templates/<uuid:pk>/delete/", views.template_delete, name="template_delete"),
    path("payment/<uuid:customer_pk>/", views.customer_payment, name="customer_payment"),
    path("<uuid:pk>/", views.invoice_detail, name="detail"),
    path("<uuid:pk>/update/", views.invoice_update, name="update"),
    path("<uuid:pk>/shipping/", views.shipping, name="shipping"),
    path("<uuid:pk>/lines/", views.line_add, name="line_add"),
    path("<uuid:pk>/lines/<uuid:line_pk>/delete/", views.line_delete, name="line_delete"),
    path("<uuid:pk>/pull/", views.pull, name="pull"),
    path("<uuid:pk>/issue/", views.issue, name="issue"),
    path("<uuid:pk>/void/", views.void, name="void"),
    path("<uuid:pk>/delete/", views.delete_draft, name="delete"),
    path("<uuid:pk>/pay/", views.pay, name="pay"),
    path("<uuid:pk>/apply-credit/", views.apply_credit, name="apply_credit"),
    path("<uuid:pk>/write-off/", views.write_off, name="write_off"),
    path("<uuid:pk>/credit-note/", views.credit_note, name="credit_note"),
    path("<uuid:pk>/document/", views.document, name="document"),
    path("<uuid:pk>/pdf/", views.pdf, name="pdf"),
]
