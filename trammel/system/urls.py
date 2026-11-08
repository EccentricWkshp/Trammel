from django.urls import path

from . import views

app_name = "system"

urlpatterns = [
    path("", views.maintenance, name="maintenance"),
    path("backup/", views.backup_now, name="backup_now"),
    path("backup/test-s3/", views.test_s3, name="test_s3"),
    path("backup/<uuid:pk>/download/", views.download_backup, name="download_backup"),
    path("backup/<uuid:pk>/restore/", views.restore_confirm, name="restore"),
    path("export/", views.export_now, name="export_now"),
    path("export/<uuid:pk>/download/", views.download_export, name="download_export"),
    path("updates/", views.check_updates, name="check_updates"),
    path("trash/", views.trash_list, name="trash"),
    path("trash/<str:label>/<uuid:pk>/restore/", views.trash_restore, name="trash_restore"),
]
