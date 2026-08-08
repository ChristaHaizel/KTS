"""Creating in-system notifications.

Delivery is in-system only. That is what the requirement asks for, and it is
also the only thing a host with no outbound mail can actually honour - a
notification that silently fails to send is worse than none.
"""
import logging

from django.contrib.auth import get_user_model

from .models import Notification, Student

logger = logging.getLogger(__name__)


def notify(users, message):
    """Send one message to many accounts in a single insert."""
    recipients = [u for u in users if u is not None]
    if not recipients:
        return 0
    Notification.objects.bulk_create(
        [Notification(user=user, message=message) for user in recipients]
    )
    logger.info('Notified %d account(s): %s', len(recipients), message)
    return len(recipients)


def accounts_in_group(group):
    """Signed-up students in a group. Students without an account are skipped:
    there is nowhere to deliver to."""
    if group is None:
        return []
    User = get_user_model()
    return list(
        User.objects.filter(
            student_profile__in=Student.objects.filter(group=group)
        )
    )


def notify_group_of_change(group, message):
    return notify(accounts_in_group(group), message)


def notify_all_students(message):
    User = get_user_model()
    return notify(list(User.objects.filter(student_profile__isnull=False)), message)
