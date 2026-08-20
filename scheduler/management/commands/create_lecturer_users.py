from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.accounts import generate_password, lecturer_username
from scheduler.models import Lecturer


class Command(BaseCommand):
    help = (
        'Create login accounts for lecturers who do not have one, and link them. '
        'Passwords are generated and printed once - run this from a terminal you '
        'trust, never as part of an automated build.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would be created without writing anything.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        dry_run = options['dry_run']

        pending = list(Lecturer.objects.filter(user__isnull=True).order_by('name'))
        if not pending:
            total = Lecturer.objects.count()
            self.stdout.write(
                f'Every lecturer already has an account ({total} total). Nothing to do.'
            )
            return

        taken = set(User.objects.values_list('username', flat=True))
        created = []

        for lecturer in pending:
            username = lecturer_username(lecturer, taken)
            taken.add(username)
            password = generate_password()

            if not dry_run:
                user = User.objects.create_user(
                    username=username, email=lecturer.email, password=password
                )
                lecturer.user = user
                lecturer.save(update_fields=['user'])

            created.append((lecturer.name, username, password))

        self.stdout.write('')
        self.stdout.write(f"{'Lecturer':<24}{'Username':<18}{'Password':<16}")
        self.stdout.write('-' * 58)
        for name, username, password in created:
            self.stdout.write(f'{name:<24}{username:<18}{password:<16}')

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Dry run - nothing was written and these passwords were not set.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Created {len(created)} account(s).'
            ))
            self.stdout.write(self.style.WARNING(
                'These passwords are shown once and are not recoverable. Hand them '
                'over securely and have each lecturer change theirs. Accounts have '
                'no admin rights: they can view the timetable and raise reschedule '
                'requests for their own classes only.'
            ))
