from django.urls import path

from trammel.api.api import api
from trammel.core.navigation import module_urlpatterns
from trammel.core.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("api/v1/", api.urls),
    *module_urlpatterns(),
]
