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


#: Session key holding when the person signed in.
SIGNED_IN_AT = "signed_in_at"


def mark_signed_in(sender, request, user, **kwargs):
    """Connected to user_logged_in: the start of the session's absolute lifetime."""
    from django.utils import timezone

    request.session[SIGNED_IN_AT] = timezone.now().isoformat()


class SessionLifetimeMiddleware:
    """Signs a person out SESSION_ABSOLUTE_DAYS after they signed in, however
    active the session has been. A session from before this started counting
    starts counting now."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import datetime as dt

        from django.conf import settings
        from django.contrib import messages
        from django.contrib.auth import logout
        from django.utils import timezone

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            started = request.session.get(SIGNED_IN_AT)
            try:
                since = dt.datetime.fromisoformat(started) if started else None
            except TypeError, ValueError:
                since = None
            if since is None:
                request.session[SIGNED_IN_AT] = timezone.now().isoformat()
            elif timezone.now() - since > dt.timedelta(days=settings.SESSION_ABSOLUTE_DAYS):
                logout(request)
                if not request.path.startswith("/api/"):
                    messages.info(request, "For security, please sign in again.")
                    from django.contrib.auth.views import redirect_to_login

                    return redirect_to_login(request.get_full_path())
        return self.get_response(request)
