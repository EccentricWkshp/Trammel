from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    path("attach/<str:label>/<uuid:object_id>/", views.attach_upload, name="attach"),
    path("<uuid:pk>/revision/", views.new_revision, name="new_revision"),
    path("<uuid:pk>/thumbnail/", views.thumbnail, name="thumbnail"),
    path("<uuid:pk>/thumbnail/status/", views.thumbnail_status, name="thumbnail_status"),
    path("revision/<uuid:pk>/", views.download, name="download"),
    path("link/<uuid:pk>/detach/", views.detach, name="detach"),
    path("link/<uuid:pk>/share/", views.toggle_share, name="toggle_share"),
    path("<uuid:pk>/edit/", views.document_edit, name="edit"),
    path("certificate/", views.use_as_certificate, name="use_as_certificate"),
    path("shop/<slug:key>/", views.shop_document_upload, name="shop_document"),
    path("types/", views.type_list, name="types"),
    path("types/new/", views.type_edit, name="type_create"),
    path("types/<uuid:pk>/", views.type_edit, name="type_edit"),
]
