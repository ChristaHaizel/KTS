import os

from django.core.management import call_command
from django.core.management.base import BaseCommand

from scheduler.models import Course, Room, TimeSlot


class Command(BaseCommand):
    help = (
        'Seed the demo dataset during deploy, for hosts with no shell access. '
        'Does nothing unless SEED_DEMO is set, and nothing if the database '
        'already holds data - so it is safe to leave in the build command.'
    )

    def handle(self, *args, **options):
        if os.environ.get('SEED_DEMO', '').lower() not in ('1', 'true', 'yes'):
            self.stdout.write('SEED_DEMO is not set - skipping demo data.')
            return

        # Refuse on a database that already has content. A deploy must never be
        # able to overwrite real data, and an env var left switched on by
        # accident should be inert rather than destructive.
        existing = Course.objects.count() + Room.objects.count() + TimeSlot.objects.count()
        if existing:
            self.stdout.write(self.style.WARNING(
                f'Database already holds {existing} reference record(s) - leaving it '
                'alone. Unset SEED_DEMO; seeding only ever runs on an empty database.'
            ))
            return

        self.stdout.write('SEED_DEMO is set and the database is empty - seeding.')
        call_command('seed_demo')
        self.stdout.write(self.style.SUCCESS(
            'Demo data created. Give people logins with the Create account buttons '
            'on the Lecturers and Students pages - passwords appear in the browser, '
            'never in this log. Unset SEED_DEMO now.'
        ))
