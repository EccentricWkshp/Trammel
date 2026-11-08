from django.urls import path

from . import views

app_name = "agreements"

urlpatterns = [
    path("templates/", views.template_list, name="templates"),
    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<uuid:pk>/", views.template_edit, name="template_edit"),
    path("settings/", views.settings_edit, name="settings"),
    path("job/<uuid:job_pk>/issue/", views.issue, name="issue"),
    path("job/<uuid:job_pk>/waive/", views.waive, name="waive"),
    path("<uuid:pk>/", views.detail, name="detail"),
    path("<uuid:pk>/pdf/", views.pdf, name="pdf"),
    path("<uuid:pk>/accept/", views.accept, name="accept"),
]
