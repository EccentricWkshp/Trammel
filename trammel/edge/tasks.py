from datetime import timedelta

from trammel.core.tasks import periodic, task


@periodic("edge.sync", every=timedelta(seconds=30))
def sync(payload):
    """Push what changed to the edge portal and apply what customers did there."""
    from . import deploy as edge_deploy
    from . import sync as edge_sync

    edge_deploy.finish_waiting()
    edge_sync.run()


@task("edge.deploy")
def deploy(payload):
    from . import deploy as edge_deploy

    edge_deploy.run_deploy()


@periodic("edge.redeploy", every=timedelta(hours=1))
def redeploy(payload):
    """After Trammel is updated, update the Worker to match."""
    from django.conf import settings

    from .models import EdgeSettings

    row = EdgeSettings.load()
    if row.active and not row.deploying and row.deployed_version != settings.APP_VERSION:
        from . import deploy as edge_deploy

        edge_deploy.run_deploy()
