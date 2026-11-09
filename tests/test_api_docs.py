"""The API's OpenAPI document, which the docs page renders (FR-API-1)."""

import pytest


@pytest.mark.django_db
def test_openapi_schema_builds(admin_client):
    response = admin_client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/jobs/" in paths and "/api/v1/customers/" in paths


@pytest.mark.django_db
def test_jobs_list_with_status_filter(admin_client):
    # The status filter's type must resolve when the paginated view is built;
    # in 0.1.0 it did not, and every /jobs/ listing failed with a 500.
    assert admin_client.get("/api/v1/jobs/").status_code == 200
    assert admin_client.get("/api/v1/jobs/?status=proposal").status_code == 200
    assert admin_client.get("/api/v1/jobs/?status=nonsense").status_code == 422


def test_schema_names_do_not_collide(monkeypatch):
    # Ninja merges every model's schema definitions by bare class name, so two
    # different schemas (or enums) with one name silently become one, and the
    # docs show the wrong fields or values for some endpoints.
    from ninja.openapi.schema import OpenAPISchema

    from trammel.api.api import api

    collisions = []
    original = OpenAPISchema.add_schema_definitions

    def checking(self, definitions):
        for name, definition in definitions.items():
            if name in self.schemas and self.schemas[name] != definition:
                collisions.append(name)
        original(self, definitions)

    monkeypatch.setattr(OpenAPISchema, "add_schema_definitions", checking)
    api.get_openapi_schema(path_prefix="/api/v1/")
    assert collisions == []
