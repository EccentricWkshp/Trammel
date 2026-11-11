"""Keeping calculated product costs current when what they are made from changes."""

from django.db.models.signals import post_save

from trammel.core.models import ShopRates
from trammel.shop.models import Machine

from . import costing
from .models import Product


def _enabled() -> bool:
    from trammel.core import modules

    return modules.is_enabled("catalog")


def _machine_saved(sender, instance, created, **kwargs):
    """A machine's rates changed: products whose routing uses it are costed again."""
    if not created and _enabled():
        costing.refresh(Product.objects.filter(routing_steps__machine=instance).distinct())


def _rates_saved(sender, instance, created, **kwargs):
    """The shop's labor or overhead rate changed: every routing is costed again."""
    if not created and _enabled():
        costing.refresh(Product.objects.filter(routing_steps__isnull=False).distinct())


post_save.connect(_machine_saved, sender=Machine, dispatch_uid="catalog.machine_cost")
post_save.connect(_rates_saved, sender=ShopRates, dispatch_uid="catalog.rates_cost")
