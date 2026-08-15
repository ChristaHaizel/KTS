"""Signing in with an email address as well as a username.

A student's username is their student ID, so that already works through
Django's own backend. This adds the other half of the promise - that either
will do - without touching how passwords are checked.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    """Resolve an email address to an account, then defer to the usual checks.

    Sits after the default backend, which has already tried the value as a
    username. Only what looks like an address gets this far, so an ordinary
    username never costs an extra query.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        username = username or kwargs.get(get_user_model().USERNAME_FIELD)
        if not username or '@' not in username or not password:
            return None

        User = get_user_model()
        # Two, so that a second match can be noticed. Email is not unique on an
        # account, and guessing which of two people meant to sign in is not
        # something to do quietly.
        matches = list(User.objects.filter(email__iexact=username)[:2])

        if len(matches) != 1:
            # Hash anyway. Returning early on an unknown address makes it
            # measurably faster than a known one, which is a way of asking the
            # site which addresses have accounts.
            User().set_password(password)
            return None

        user = matches[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
