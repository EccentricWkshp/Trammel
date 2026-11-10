from django import forms
from django.db import connection

from trammel.customers.models import Customer

from .models import Job

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def tags_in_use() -> list[str]:
    """Every tag on a live job, most used first — offered as choices on the job form."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tag FROM jobs_job, unnest(tags) AS tag WHERE deleted_at IS NULL "
            "GROUP BY tag ORDER BY count(*) DESC, tag"
        )
        return [row[0] for row in cursor.fetchall()]


def split_tags(value: str) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


class JobForm(forms.ModelForm):
    # The walk-in placeholder is the empty choice, not a second entry in the list.
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.filter(is_placeholder=False, is_internal=False).order_by("name"),
        required=False,
        empty_label="Walk-in / not known yet",
        help_text="A walk-in placeholder can be replaced with the real customer later.",
    )
    internal = forms.BooleanField(
        label="Internal shop job",
        required=False,
        help_text="Work for the shop itself. It needs no quote approval or agreement, "
        "and is never invoiced.",
    )
    tags = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "tag-choices"}),
        help_text="Tags already used on other jobs, most common first.",
    )
    new_tags = forms.CharField(
        label="New tags",
        required=False,
        help_text="Comma-separated, for tags not in the list above.",
    )

    # "tags" is set in save() from both pickers, so it stays out of Meta.fields.
    field_order = [
        "title",
        "customer",
        "internal",
        "priority",
        "description",
        "tags",
        "new_tags",
        "target_completion",
        "due_date",
        "promised_date",
        "delivery_planned",
        "delivery_actual",
    ]

    class Meta:
        model = Job
        fields = [
            "title",
            "customer",
            "priority",
            "description",
            "target_completion",
            "due_date",
            "promised_date",
            "delivery_planned",
            "delivery_actual",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "target_completion": DATE,
            "due_date": DATE,
            "promised_date": DATE,
            "delivery_planned": DATE,
            "delivery_actual": DATE,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = list(self.instance.tags or []) if self.instance.pk else []
        known = tags_in_use()
        # The job's own tags are always offered, even if no other job uses them.
        choices = known + [t for t in current if t not in known]
        self.fields["tags"].choices = [(t, t) for t in choices]
        if not choices:
            del self.fields["tags"]
            self.fields["new_tags"].label = "Tags"
            self.fields["new_tags"].help_text = "Comma-separated, e.g. “laser, signage”."
        else:
            self.initial["tags"] = current
        # A new job already has an id (UUIDv7 default), so test for a customer.
        if self.instance.customer_id and (
            self.instance.customer.is_placeholder or self.instance.customer.is_internal
        ):
            self.initial["customer"] = None
        if self.instance.customer_id and self.instance.customer.is_internal:
            self.initial["internal"] = True
        # Leaving priority out keeps the job's own (Normal for a new job).
        self.fields["priority"].required = False

    def clean_customer(self):
        return self.cleaned_data.get("customer") or Customer.placeholder()

    def clean_priority(self):
        value = self.cleaned_data.get("priority")
        return self.instance.priority if value in (None, "") else value

    def clean(self):
        data = super().clean()
        if data.get("internal"):
            data["customer"] = Customer.internal()
        self.all_tags = list(data.get("tags") or []) + split_tags(data.get("new_tags", ""))
        return data

    def save(self, commit=True):
        self.instance.tags = self.all_tags
        return super().save(commit=commit)


class TransitionForm(forms.Form):
    target = forms.CharField()
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
