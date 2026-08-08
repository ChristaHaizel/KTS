"""Letting an administrator preview the app as a lecturer or a student.

Signing out and back in to check the other two views is slow, and during a
demonstration it is worse than slow. This swaps the request user for the
duration of a request, while the session still authenticates the real
administrator.

The guards matter more than the feature:

- only an administrator can start it, checked on the real signed-in account
- the target can never be another administrator, so impersonating is only ever
  a loss of privilege, never a gain
- both of those are re-checked on every single request, not just when it
  starts, so revoking someone's admin rights ends any session they left open
- the banner is unconditional, because a preview the viewer forgets they are
  in is how mistakes get made
"""
import logging

from django.contrib.auth import get_user_model

from .permissions import is_admin

logger = logging.getLogger(__name__)

SESSION_KEY = 'impersonate_user_id'


class ImpersonationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.impersonator = None

        target_id = request.session.get(SESSION_KEY)
        if target_id and request.user.is_authenticated:
            real_user = request.user
            # Re-checked per request: the right to preview is not something the
            # session gets to remember on the account's behalf.
            if is_admin(real_user):
                target = get_user_model().objects.filter(pk=target_id).first()
                if target is not None and not is_admin(target):
                    request.impersonator = real_user
                    request.user = target
                else:
                    request.session.pop(SESSION_KEY, None)
            else:
                request.session.pop(SESSION_KEY, None)

        return self.get_response(request)
