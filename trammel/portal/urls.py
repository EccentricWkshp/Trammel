from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.home, name="home"),
    path("sign-in/", views.sign_in, name="sign_in"),
    path("sign-in/code/", views.code, name="code"),
    path("sign-in/choose/", views.choose, name="choose"),
    path("sign-in/access-code/", views.access_code, name="access_code"),
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
    path("files/<uuid:pk>/thumbnail/", views.file_thumbnail, name="file_thumbnail"),
    path(
        "files/<uuid:pk>/thumbnail/status/",
        views.file_thumbnail_status,
        name="file_thumbnail_status",
    ),
    path("send/", views.send_files, name="send_files"),
    path("sent/<uuid:pk>/", views.sent_file, name="sent_file"),
    path("sent/<uuid:pk>/thumbnail/", views.sent_thumbnail, name="sent_thumbnail"),
    path(
        "sent/<uuid:pk>/thumbnail/status/",
        views.sent_thumbnail_status,
        name="sent_thumbnail_status",
    ),
    path("notes/<uuid:pk>/photo/", views.note_photo, name="note_photo"),
    # Staff
    path("access/<uuid:customer_pk>/", views.set_access, name="set_access"),
    path("codes/<uuid:customer_pk>/", views.make_code, name="make_code"),
    path("codes/<uuid:customer_pk>/cancel/", views.cancel_code, name="cancel_code"),
    path("review/", views.review_list, name="review_list"),
    path("review/<uuid:pk>/", views.review, name="review"),
    path("upload-settings/", views.upload_settings, name="upload_settings"),
    path("preview/<uuid:customer_pk>/", views.preview, name="preview"),
    path("preview/stop/", views.preview_stop, name="preview_stop"),
]
