from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("sign-in/", views.sign_in, name="sign_in"),
    path("sign-in/code/", views.code, name="code"),
    path("sign-in/choose/", views.choose, name="choose"),
    path("sign-out/", views.sign_out, name="sign_out"),
    path("jobs/<uuid:pk>/", views.job, name="job"),
    path("quotes/<uuid:pk>/", views.quote, name="quote"),
    path("quotes/<uuid:pk>/respond/", views.respond, name="respond"),
    path("quotes/<uuid:pk>/document/", views.quote_document, name="quote_document"),
    path("quotes/<uuid:pk>/pdf/", views.quote_pdf, name="quote_pdf"),
    path("invoices/<uuid:pk>/", views.invoice, name="invoice"),
    path("invoices/<uuid:pk>/document/", views.invoice_document, name="invoice_document"),
    path("invoices/<uuid:pk>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("agreements/<uuid:pk>/pdf/", views.agreement_pdf, name="agreement_pdf"),
    path("files/<uuid:pk>/", views.file, name="file"),
    # Staff
    path("access/<uuid:customer_pk>/", views.set_access, name="set_access"),
]
