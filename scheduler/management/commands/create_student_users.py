from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.accounts import create_student_account
from scheduler.models import Student


class Command(BaseCommand):
    help = (
        'Create login accounts for students who do not have one. The username is '
        'the student ID. Passwords are generated and printed once - run this from '
        'a terminal you trust, never as part of an automated build.'
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

        pending = list(Student.objects.filter(user__isnull=True).order_by('student_id'))
        if not pending:
            self.stdout.write(
                f'Every student already has an account ({Student.objects.count()} total). '
                'Nothing to do.'
            )
            return

        created, skipped = [], []
        for student in pending:
            # The student ID is the username, so a collision means the ID is
            # already spoken for and needs a human to look at it.
            if User.objects.filter(username=student.student_id).exists():
                skipped.append(student)
                continue
            if dry_run:
                created.append((student.student_id, student.name, '(not set)'))
                continue
            _user, password = create_student_account(student)
            created.append((student.student_id, student.name, password))

        if created:
            self.stdout.write('')
            self.stdout.write(f"{'Username (student ID)':<24}{'Name':<24}{'Password':<16}")
            self.stdout.write('-' * 64)
            for student_id, name, password in created:
                self.stdout.write(f'{student_id:<24}{name:<24}{password:<16}')

        if skipped:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR(
                f'{len(skipped)} skipped - an account already exists with that '
                'student ID but is not linked to the student record: '
                + ', '.join(s.student_id for s in skipped)
            ))

        self.stdout.write('')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'Dry run - nothing was written and no passwords were set.'
            ))
        elif created:
            self.stdout.write(self.style.SUCCESS(f'Created {len(created)} account(s).'))
            self.stdout.write(self.style.WARNING(
                'These passwords are shown once and are not recoverable. Students '
                'sign in with their student ID as the username. Accounts can see '
                'their own timetable and their notifications, nothing else.'
            ))
