"""Form rendering: Django's templates, with one field layout used everywhere.

`partials/field.html` puts a checkbox before its label on the same line and
otherwise matches Django's own field template.
"""

from django.forms.renderers import TemplatesSetting


class TrammelFormRenderer(TemplatesSetting):
    field_template_name = "partials/field.html"
