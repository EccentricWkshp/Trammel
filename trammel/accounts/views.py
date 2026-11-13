from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode
from django.views.decorators.http import require_POST

from trammel.core import access, audit
from trammel.core.middleware import client_ip
from trammel.core.web import admin_required, permission_required

from . import mfa, security, services
from .forms import ApiKeyForm, LoginForm, ProfileForm, RoleForm, UserForm
from .models import ApiKey, MagicLink, Role, User

# ---------------------------------------------------------------------------
# Sign-in (SPEC §9)
# ---------------------------------------------------------------------------


def login_view(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().lower()
        ip = client_ip(request)
        if security.is_locked(email, ip):
            if not security.recently_logged_lock(email):
                audit.record(
                    "auth.locked",
                    entity_type="accounts.user",
                    entity_label=email,
                    summary=f"Sign-in refused for {email}: too many failures",
                )
            form.add_error(None, security.LOCKED_MESSAGE)
        else:
            user = authenticate(request, username=email, password=form.cleaned_data["password"])
            if user is None:
                security.record_attempt(email, ip, succeeded=False)
                audit.record(
                    "auth.login_failed",
                    entity_type="accounts.user",
                    entity_label=email,
                    summary=f"Failed sign-in for {email}",
                )
                form.add_error(None, "That email address and password do not match.")
            else:
                return _first_factor_passed(request, user, method="password")
    return render(
        request, "accounts/login.html", {"form": form, "magic_available": _email_available()}
    )


def _safe_next(request) -> str:
    target = request.GET.get("next", "") or request.session.get("mfa_next", "")
    if not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        target = settings.LOGIN_REDIRECT_URL
    return target


def _first_factor_passed(request, user, *, method: str):
    """After a password or magic link: ask for the second factor if the user has one."""
    if mfa.enabled(user):
        request.session["mfa_user"] = str(user.pk)
        request.session["mfa_method"] = method
        request.session["mfa_started"] = timezone.now().timestamp()
        request.session["mfa_next"] = request.GET.get("next", "")
        request.session["mfa_tries"] = 0
        return redirect("accounts:mfa_verify")
    return _complete_login(request, user, method=method)


#: Wrong second-factor codes one password (or email link) allows before
#: signing in has to start again (SEC-04, 0.10.5).
MFA_TRIES = 5


def _complete_login(request, user, *, method: str):
    # Only now, with every factor passed, does a success clear the failures.
    security.record_attempt(user.email, client_ip(request), succeeded=True)
    target = _safe_next(request)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    audit.record(
        "auth.login", user, summary=f"{user} signed in ({method})", actor=access.as_actor(user)
    )
    return redirect(target)


def mfa_verify(request):
    """The second step of sign-in (SPEC §9)."""
    pk = request.session.get("mfa_user")
    started = request.session.get("mfa_started", 0)
    if not pk or timezone.now().timestamp() - started > 300:
        request.session.pop("mfa_user", None)
        return redirect("accounts:login")
    user = User.objects.filter(pk=pk, is_active=True).first()
    if user is None:
        return redirect("accounts:login")
    error = ""
    if request.method == "POST":
        ip = client_ip(request)
        if security.is_locked(user.email, ip):
            error = security.LOCKED_MESSAGE
        elif mfa.verify(user, request.POST.get("code", "")):
            method = request.session.pop("mfa_method", "password")
            request.session.pop("mfa_user", None)
            return _complete_login(request, user, method=f"{method} + authenticator")
        else:
            security.record_attempt(user.email, ip, succeeded=False)
            audit.record(
                "auth.mfa_failed",
                user,
                summary=f"Wrong second-factor code for {user}",
                actor=access.ANONYMOUS,
            )
            tries = int(request.session.get("mfa_tries", 0)) + 1
            if tries >= MFA_TRIES:
                for key in ("mfa_user", "mfa_method", "mfa_started", "mfa_tries"):
                    request.session.pop(key, None)
                messages.error(request, "Too many wrong codes. Sign in again to try once more.")
                return redirect("accounts:login")
            request.session["mfa_tries"] = tries
            error = "That code is not right. Try the current one from your authenticator app."
    return render(request, "accounts/mfa_verify.html", {"error": error})


def _email_available() -> bool:
    try:
        from trammel.notifications.models import SmtpSettings
    except ImportError:  # pragma: no cover
        return False
    return SmtpSettings.load().is_configured or bool(getattr(settings, "TRAMMEL_EMAIL_BACKEND", ""))


def magic_request(request):
    """Email a single-use sign-in link. Same answer whether or not the address exists."""
    if request.method == "POST" and _email_available():
        email = request.POST.get("email", "").strip().lower()[:254]
        # Looked up and sent by the worker: the answer takes the same time
        # whether or not the address has an account.
        from trammel.core import tasks

        tasks.enqueue_public("accounts.magic_link", email, client_ip(request))
        messages.success(
            request,
            "If that address belongs to an account, a sign-in link is on "
            "its way. It works once and expires in 15 minutes.",
        )
        return redirect("accounts:login")
    return redirect("accounts:login")


def magic_redeem(request, token):
    """Opening the link shows a button; only pressing it signs in. Mail scanners
    that open links don't use them up, and a link can't sign someone in
    without them choosing to."""
    if request.method != "POST":
        if not MagicLink.is_usable(token):
            return render(request, "accounts/magic_invalid.html", status=400)
        return render(request, "accounts/magic_confirm.html")
    user = MagicLink.redeem(token)
    if user is None:
        return render(request, "accounts/magic_invalid.html", status=400)
    return _first_factor_passed(request, user, method="email link")


@login_required
def mfa_setup(request):
    """Set up an authenticator app (SPEC §9)."""
    user = request.user
    secret = request.session.get("mfa_new_secret") or mfa.new_secret()
    request.session["mfa_new_secret"] = secret
    error = ""
    if request.method == "POST":
        step = mfa.matching_step(secret, request.POST.get("code", ""))
        if mfa.enabled(user) and not mfa.verify(user, request.POST.get("current_code", "")):
            # Someone else holding the session must not swap in their own phone.
            error = "Enter a current code from the authenticator you use now, or a recovery code."
        elif step is None:
            error = "That code does not match. Check the time on your phone and try again."
        else:
            codes = mfa.enable(user, secret)
            request.session.pop("mfa_new_secret", None)
            audit.record("auth.mfa_enabled", user, summary=f"{user} turned on two-factor sign-in")
            return render(request, "accounts/mfa_codes.html", {"codes": codes})
    from trammel.core.qr import svg as qr_svg

    uri = mfa.provisioning_uri(
        secret, user.email, request.business.name if request.business else "Trammel"
    )
    return render(
        request,
        "accounts/mfa_setup.html",
        {
            "secret": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
            "qr": qr_svg(uri),
            "error": error,
            "required": mfa.required(user),
            "enabled": mfa.enabled(user),
        },
    )


@login_required
@require_POST
def mfa_disable(request):
    user = request.user
    if mfa.required(user):
        messages.error(request, "Your role requires two-factor sign-in.")
    elif not mfa.verify(user, request.POST.get("code", "")):
        messages.error(request, "Enter a current code to turn two-factor sign-in off.")
    else:
        mfa.disable(user)
        audit.record("auth.mfa_disabled", user, summary=f"{user} turned off two-factor sign-in")
        messages.success(request, "Two-factor sign-in is off.")
    return redirect("accounts:profile")


@admin_required
@require_POST
def user_mfa_reset(request, pk):
    """FR-ADM-5: reset a user's MFA, e.g. after a lost phone."""
    user = get_object_or_404(User, pk=pk)
    mfa.disable(user)
    audit.record("auth.mfa_reset", user, summary=f"Reset two-factor sign-in for {user}")
    messages.success(request, f"Two-factor sign-in reset for {user}.")
    return redirect("accounts:user_edit", pk=user.pk)


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit.record("auth.logout", request.user, summary=f"{request.user} signed out")
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


def set_password(request, uidb64, token):
    """Choose a password from a one-time link (invite or reset)."""
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uidb64)), is_active=True)
    except User.DoesNotExist, ValueError, TypeError, OverflowError, ValidationError:
        user = None
    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "accounts/set_password_invalid.html", status=400)
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.record(
            "user.password_set",
            user,
            summary=f"{user} set a new password",
            actor=access.as_actor(user),
        )
        messages.success(request, "Password set. You can sign in now.")
        return redirect("accounts:login")
    return render(request, "accounts/set_password.html", {"form": form, "target": user})


# ---------------------------------------------------------------------------
# Own profile and API keys
# ---------------------------------------------------------------------------


@login_required
def profile(request):
    user = request.user
    before = audit.snapshot(user)
    form = ProfileForm(request.POST or None, instance=user, prefix="profile")
    if request.method == "POST" and "profile-full_name" in request.POST and form.is_valid():
        form.save()
        audit.record_update(user, before, summary=f"{user} edited their profile")
        messages.success(request, "Profile saved.")
        return redirect("accounts:profile")
    keys = user.api_keys.all() if request.user.has_perm("accounts.api_keys") else None
    return render(request, "accounts/profile.html", {"form": form, "api_keys": keys})


@login_required
def change_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        update_session_auth_hash(request, form.user)
        audit.record(
            "user.password_change", request.user, summary=f"{request.user} changed their password"
        )
        messages.success(request, "Password changed.")
        return redirect("accounts:profile")
    return render(
        request,
        "core/settings_form.html",
        {"form": form, "title": "Change password", "back": "accounts:profile"},
    )


@permission_required("accounts.api_keys")
def api_key_create(request):
    held = access.permissions_of(request.actor)
    form = ApiKeyForm(request.POST or None, held=held)
    token = None
    if request.method == "POST" and form.is_valid():
        try:
            key, token = ApiKey.issue(
                request.user,
                form.cleaned_data["name"],
                form.cleaned_data["scopes"],
                form.cleaned_data["expires_in_days"],
            )
        except ValidationError as exc:
            form.add_error("scopes", exc)
        else:
            audit.record(
                "api_key.create",
                key,
                summary=f"Created API key “{key.name}”",
                after={"scopes": key.scopes, "expires_at": key.expires_at},
            )
    return render(request, "accounts/api_key_form.html", {"form": form, "token": token})


@login_required
@require_POST
def api_key_revoke(request, pk):
    key = get_object_or_404(ApiKey, pk=pk)
    if key.user_id != request.user.pk and not access.is_admin(request.actor):
        return redirect("accounts:profile")
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at"])
        audit.record("api_key.revoke", key, summary=f"Revoked API key “{key.name}”")
        messages.success(request, f"API key “{key.name}” revoked.")
    return redirect("accounts:profile")


# ---------------------------------------------------------------------------
# User administration (FR-ADM-5) — system admin only
# ---------------------------------------------------------------------------


@admin_required
def user_list(request):
    users = User.objects.prefetch_related("roles").order_by("-is_active", "full_name")
    return render(request, "accounts/users.html", {"users": users})


@admin_required
def user_edit(request, pk=None):
    user = get_object_or_404(User, pk=pk) if pk else User()
    creating = pk is None
    before = None if creating else audit.snapshot(user)
    form = UserForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        try:
            user = form.save(commit=False)
            services.save_user(user, before=before, roles=list(form.cleaned_data["roles"]))
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if creating:
                link = services.password_set_link(user, settings.BASE_URL)
                messages.success(request, f"{user} added.")
                return render(
                    request, "accounts/password_link.html", {"target": user, "link": link}
                )
            messages.success(request, f"{user} saved.")
            return redirect("accounts:users")
    return render(
        request, "accounts/user_form.html", {"form": form, "target": user, "creating": creating}
    )


@admin_required
@require_POST
def user_set_active(request, pk):
    user = get_object_or_404(User, pk=pk)
    try:
        services.set_active(user, request.POST.get("active") == "1", by=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"{user} {'reactivated' if user.is_active else 'deactivated'}.")
    return redirect("accounts:users")


@admin_required
@require_POST
def user_password_link(request, pk):
    user = get_object_or_404(User, pk=pk, is_active=True)
    link = services.password_set_link(user, settings.BASE_URL)
    return render(request, "accounts/password_link.html", {"target": user, "link": link})


# ---------------------------------------------------------------------------
# Roles (FR-ADM-6) — system admin only
# ---------------------------------------------------------------------------


@admin_required
def role_list(request):
    roles = Role.objects.prefetch_related("users")
    return render(request, "accounts/roles.html", {"roles": roles})


@admin_required
def role_edit(request, pk=None):
    role = get_object_or_404(Role, pk=pk) if pk else Role()
    before = None if pk is None else audit.snapshot(role)
    form = RoleForm(request.POST or None, instance=role)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_role(form.save(commit=False), before=before)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Role “{role}” saved.")
            return redirect("accounts:roles")
    return render(request, "accounts/role_form.html", {"form": form, "role": role})


@admin_required
@require_POST
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    with transaction.atomic():
        services.delete_role(role)
    messages.success(request, f"Role “{role.name}” deleted.")
    return redirect("accounts:roles")
