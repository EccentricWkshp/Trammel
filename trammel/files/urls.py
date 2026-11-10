from django.urls import path

from . import views

app_name = "files"

urlpatterns = [
    path("attach/<str:label>/<uuid:object_id>/", views.attach_upload, name="attach"),
    path("<uuid:pk>/revision/", views.new_revision, name="new_revision"),
    path("<uuid:pk>/thumbnail/", views.thumbnail, name="thumbnail"),
    path("revision/<uuid:pk>/", views.download, name="download"),
    path("link/<uuid:pk>/detach/", views.detach, name="detach"),
    path("link/<uuid:pk>/share/", views.toggle_share, name="toggle_share"),
    path("<uuid:pk>/edit/", views.document_edit, name="edit"),
    path("types/", views.type_list, name="types"),
    path("types/new/", views.type_edit, name="type_create"),
    path("types/<uuid:pk>/", views.type_edit, name="type_edit"),
]
