"""On upgrade to 0.5.0, a product whose cost was set from its bill of materials is
calculated from then on; every other product keeps its cost as an override."""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

D = Decimal
BEFORE = [("catalog", "0003_routing"), ("stock", "0006_low_stock_notice")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


@pytest.mark.django_db(transaction=True)
def test_costs_carry_over():
    try:
        _check(_migrate(BEFORE))
    finally:
        _migrate(_latest())  # never leave the test database behind


def _check(apps):
    Product = apps.get_model("catalog", "Product")
    ProductCosting = apps.get_model("stock", "ProductCosting")
    board = Product.objects.create(name="Board", cost=D("52"))
    ProductCosting.objects.create(product=board, materials=D("32"), other=D("20"))
    oil = Product.objects.create(name="Board butter", kind="resold", cost=D("5.5"))

    apps = _migrate(_latest())
    Product = apps.get_model("catalog", "Product")
    board = Product.objects.get(pk=board.pk)
    assert board.cost_override is None and board.other_cost == D("20")
    oil = Product.objects.get(pk=oil.pk)
    assert oil.cost_override == D("5.5") and oil.cost == D("5.5")
