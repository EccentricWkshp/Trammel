"""Form widgets shared across modules."""

from django import forms


class MultiSelectDropdown(forms.CheckboxSelectMultiple):
    """Checkboxes inside a dropdown; the closed box lists what is chosen.

    Works without script (a native `<details>`); `trammel.js` keeps the summary
    line up to date as boxes are ticked.
    """

    template_name = "widgets/multiselect_dropdown.html"

    def __init__(self, attrs=None, choices=(), placeholder="None chosen"):
        super().__init__(attrs, choices)
        self.placeholder = placeholder

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        chosen = [
            option["label"]
            for _, options, _ in context["widget"]["optgroups"]
            for option in options
            if option["selected"]
        ]
        context["widget"]["chosen"] = chosen
        context["widget"]["placeholder"] = self.placeholder
        return context

    use_fieldset = False  # a plain label, like a select
