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


def student_for(user):
    """The Student this account belongs to, or None."""
    if not user.is_authenticated:
        return None
    return getattr(user, 'student_profile', None)


def is_student(user):
    return student_for(user) is not None


def staff_only(user):
    """Administrators and lecturers. Students are read-only on their own
    timetable and have no business in the conflict report or the reschedule
    workflow, neither of which is theirs to act on."""
    return is_admin(user) or lecturer_for(user) is not None


staff_required = user_passes_test(staff_only)


def role_context(request):
    """Context processor: who the signed-in account is, and their unread count."""
    from .models import Notification  # imported here to avoid a circular import

    user = request.user
    student = student_for(user)
    lecturer = lecturer_for(user)

    unread = 0
    if user.is_authenticated:
        unread = Notification.objects.filter(user=user, read_at__isnull=True).count()

    # A student's username is their ID, so its first character is a digit and
    # makes a meaningless avatar. Prefer the name on their record.
    profile = student or lecturer
    display_name = profile.name if profile else getattr(user, 'username', '')
    return {
        'is_admin': is_admin(user),
        'lecturer_profile': lecturer,
        'student_profile': student,
        'is_student': student is not None,
        'unread_notifications': unread,
        'display_name': display_name,
        'avatar_initial': (display_name[:1] or '?').upper(),
    }
