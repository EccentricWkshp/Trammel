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
    customer = forms.ModelChoiceField(
        queryset=Customer.objects.order_by("-is_placeholder", "name"),
        required=False,
        empty_label="Walk-in / not known yet",
        help_text="A walk-in placeholder can be replaced with the real customer later.",
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

    def clean(self):
        data = super().clean()
        self.all_tags = list(data.get("tags") or []) + split_tags(data.get("new_tags", ""))
        return data

    def save(self, commit=True):
        self.instance.tags = self.all_tags
        return super().save(commit=commit)


class TransitionForm(forms.Form):
    target = forms.CharField()
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
