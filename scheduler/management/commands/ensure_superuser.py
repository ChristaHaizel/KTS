import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from scheduler.permissions import ADMIN_GROUP


class Command(BaseCommand):
    help = (
        'Create the first superuser from DJANGO_SUPERUSER_* environment variables, '
        'for hosts with no shell access. Idempotent: does nothing when the account '
        'already exists or the variables are unset, so it is safe on every deploy.'
    )

    @transaction.atomic
    def handle(self, *args, **options):
        # The admin group gates generate/approve/edit for everyone who is not a
        # superuser, so it must exist before anyone can be added to it.
        group, group_created = Group.objects.get_or_create(name=ADMIN_GROUP)
        if group_created:
            self.stdout.write(f'Created group "{ADMIN_GROUP}".')

        username = os.environ.get('DJANGO_SUPERUSER_USERNAME', '').strip()
        email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '').strip()
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '')

        User = get_user_model()

        if not username:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME is not set - skipping superuser creation.'
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'Superuser "{username}" already exists - nothing to do.')
            return

        if not password:
            self.stdout.write(self.style.WARNING(
                f'User "{username}" does not exist, but DJANGO_SUPERUSER_PASSWORD is '
                'not set - skipping. Set it for one deploy to create the account.'
            ))
            return

        # Validate before creating so a rejected password fails loudly here rather
        # than leaving the site with no way to log in. Never echo the password.
        candidate = User(username=username, email=email)
        try:
            validate_password(password, candidate)
        except ValidationError as exc:
            raise CommandError(
                'DJANGO_SUPERUSER_PASSWORD was rejected: ' + ' '.join(exc.messages)
            )

        user = User.objects.create_superuser(
            username=username, email=email, password=password
        )
        user.groups.add(group)
        self.stdout.write(self.style.SUCCESS(
            f'Created superuser "{username}". Delete DJANGO_SUPERUSER_PASSWORD from '
            'the environment now that the account exists.'
        ))
