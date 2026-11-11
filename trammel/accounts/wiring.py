"""Signals the accounts module listens to."""

from django.contrib.auth.signals import user_logged_in

from .middleware import mark_signed_in

user_logged_in.connect(mark_signed_in, dispatch_uid="trammel.accounts.mark_signed_in")
