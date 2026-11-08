from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("set-password/<str:uidb64>/<str:token>/", views.set_password, name="set_password"),
    path("two-factor/", views.mfa_verify, name="mfa_verify"),
    path("two-factor/setup/", views.mfa_setup, name="mfa_setup"),
    path("two-factor/disable/", views.mfa_disable, name="mfa_disable"),
    path("email-link/", views.magic_request, name="magic_request"),
    path("email-link/<str:token>/", views.magic_redeem, name="magic_redeem"),
    path("profile/", views.profile, name="profile"),
    path("profile/password/", views.change_password, name="change_password"),
    path("profile/api-keys/new/", views.api_key_create, name="api_key_create"),
    path("profile/api-keys/<uuid:pk>/revoke/", views.api_key_revoke, name="api_key_revoke"),
    path("users/", views.user_list, name="users"),
    path("users/new/", views.user_edit, name="user_create"),
    path("users/<uuid:pk>/", views.user_edit, name="user_edit"),
    path("users/<uuid:pk>/active/", views.user_set_active, name="user_set_active"),
    path("users/<uuid:pk>/password-link/", views.user_password_link, name="user_password_link"),
    path("users/<uuid:pk>/reset-mfa/", views.user_mfa_reset, name="user_mfa_reset"),
    path("roles/", views.role_list, name="roles"),
    path("roles/new/", views.role_edit, name="role_create"),
    path("roles/<uuid:pk>/", views.role_edit, name="role_edit"),
    path("roles/<uuid:pk>/delete/", views.role_delete, name="role_delete"),
]
