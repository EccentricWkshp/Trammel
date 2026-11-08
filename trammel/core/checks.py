from django.core.checks import Error, register

from .modules import check_dependencies


@register()
def module_dependencies(app_configs, **kwargs):
    return [Error(problem, id="trammel.E001") for problem in check_dependencies()]
