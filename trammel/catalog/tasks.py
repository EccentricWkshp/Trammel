from datetime import timedelta

from trammel.core.tasks import periodic


@periodic("catalog.refresh_costs", every=timedelta(hours=24))
def refresh_costs(payload):
    """Bring every calculated product cost up to date, as a safety net for any
    change to a material, machine or rate that didn't refresh them at once."""
    from trammel.core import modules

    if modules.is_enabled("catalog"):
        from .costing import refresh_all

        refresh_all()
