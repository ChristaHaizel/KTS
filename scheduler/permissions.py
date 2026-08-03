from django.contrib.auth.decorators import user_passes_test

ADMIN_GROUP = 'Timetable Admin'


def is_admin(user):
    """Timetable administrators may generate, approve, and edit reference data."""
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists())
    )


admin_required = user_passes_test(is_admin)


def lecturer_for(user):
    """The Lecturer this account belongs to, or None.

    Django makes the reverse one-to-one raise a subclass of AttributeError when
    nothing is linked, so getattr's default applies cleanly.
    """
    if not user.is_authenticated:
        return None
    return getattr(user, 'lecturer_profile', None)


def admin_flag(request):
    """Context processor so templates can hide admin-only navigation."""
    return {
        'is_admin': is_admin(request.user),
        'lecturer_profile': lecturer_for(request.user),
    }
