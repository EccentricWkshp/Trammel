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
]
