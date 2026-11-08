from django.db import models

from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import BaseModel


def _no_open_parts(widget):
    if widget.open_parts:
        yield f"{widget.open_parts} part(s) still open."


class Widget(LifecycleModel, BaseModel):
    class Status(models.TextChoices):
        OPEN = "open"
        ACTIVE = "active"
        DONE = "done"
        CLOSED = "closed"

    number = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=80)
    customer = models.ForeignKey(
        "customers.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    open_parts = models.PositiveIntegerField(default=0)

    customer_scope_path = "customer_id"

    lifecycle = Lifecycle(
        field="status",
        initial="open",
        transitions=[
            Transition("open", "active"),
            Transition("active", "done", guards=(_no_open_parts,)),
            Transition("done", "closed"),
            Transition("done", "active", kind="reopen", permission="testapp.reopen"),
            Transition("closed", "done", kind="reopen", permission="testapp.reopen"),
            Transition(
                ("open", "active", "done"), "closed", kind="force", permission="testapp.force"
            ),
        ],
    )

    def __str__(self):
        return self.name
