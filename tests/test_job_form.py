"""The job form: one walk-in choice, and tags in one group (FR-JOB-1, §6 step 2)."""

import pytest

from trammel.customers.models import Customer
from trammel.jobs import services
from trammel.jobs.models import Job


@pytest.mark.django_db
def test_walk_in_is_one_choice_not_two(admin_client, customer):
    walk_in = Customer.placeholder()
    services.create_job(Job(title="Earlier walk-in", customer=walk_in, tags=["laser"]))
    page = admin_client.get("/jobs/new/").content.decode()
    assert "Walk-in / not known yet" in page
    assert f'value="{walk_in.pk}"' not in page
    assert f'value="{customer.pk}"' in page


@pytest.mark.django_db
def test_walk_in_choice_uses_the_one_placeholder(admin_client):
    walk_in = Customer.placeholder()
    response = admin_client.post("/jobs/new/", {"title": "Counter job", "customer": ""})
    assert response.status_code == 302
    assert Job.objects.get(title="Counter job").customer == walk_in
    assert Customer.all_objects.filter(is_placeholder=True).count() == 1


@pytest.mark.django_db
def test_editing_a_walk_in_job(admin_client, customer):
    job = services.create_job(Job(title="Counter job"))
    page = admin_client.get(f"/jobs/{job.pk}/edit/").content.decode()
    assert '<option value="" selected>Walk-in / not known yet</option>' in page
    # Saving it again with the walk-in choice keeps the placeholder.
    response = admin_client.post(f"/jobs/{job.pk}/edit/", {"title": "Counter job", "customer": ""})
    assert response.status_code == 302
    job.refresh_from_db()
    assert job.customer.is_placeholder
    # Choosing a real customer replaces it.
    admin_client.post(f"/jobs/{job.pk}/edit/", {"title": "Counter job", "customer": customer.pk})
    job.refresh_from_db()
    assert job.customer == customer


@pytest.mark.django_db
def test_new_tags_box_is_inside_the_tags_group(admin_client, customer):
    services.create_job(Job(title="Tagged", customer=customer, tags=["laser"]))
    page = admin_client.get("/jobs/new/").content.decode()
    group = page[page.index('<fieldset class="tag-field">') : page.index("</fieldset>")]
    assert 'name="tags"' in group and 'name="new_tags"' in group
    response = admin_client.post(
        "/jobs/new/", {"title": "Both", "tags": ["laser"], "new_tags": "walnut, signage"}
    )
    assert response.status_code == 302
    assert Job.objects.get(title="Both").tags == ["laser", "walnut", "signage"]
