from django.shortcuts import redirect

from . import mfa

_ALLOWED = ("/accounts/two-factor/", "/accounts/logout/", "/static/", "/healthz")


class MfaEnrollmentMiddleware:
    """A user whose role requires two-factor sign-in sets it up before anything else."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(_ALLOWED)
            and not mfa.enabled(user)
            and mfa.required(user)
        ):
            if request.path.startswith("/api/"):
                from django.http import JsonResponse

                return JsonResponse(
                    {"detail": "Two-factor sign-in must be set up first."}, status=403
                )
            return redirect("accounts:mfa_setup")
        return self.get_response(request)
