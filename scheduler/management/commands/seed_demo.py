from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from scheduler.models import (
    Course, Lecturer, Notification, RescheduleRequest, Room, Student, StudentGroup,
    TimeSlot, TimetableEntry,
)

LECTURERS = [
    ('Dr. Kwame Mensah', 'kmensah@knust.edu.gh'),
    ('Prof. Ama Boateng', 'aboateng@knust.edu.gh'),
    ('Dr. Yaw Asante', 'yasante@knust.edu.gh'),
    ('Dr. Akosua Owusu', 'aowusu@knust.edu.gh'),
    ('Mr. Kofi Danso', 'kdanso@knust.edu.gh'),
    ('Dr. Efua Sarpong', 'esarpong@knust.edu.gh'),
    ('Prof. Kwabena Antwi', 'kantwi@knust.edu.gh'),
    ('Ms. Adwoa Nyarko', 'anyarko@knust.edu.gh'),
]

ROOMS = [
    ('PB 001 Lecture Hall', 250),
    ('PB 012 Lecture Hall', 180),
    ('Caesar Auditorium', 400),
    ('CS Lab 1', 60),
    ('CS Lab 2', 60),
    ('SF 21', 90),
    ('SF 22', 90),
    ('NNB Seminar Room', 45),
    ('Engineering Auditorium', 300),
    ('CIT Studio', 35),
]

# (code, name, expected_students, lecturer_index)
COURSES = [
    ('CS 151', 'Introduction to Programming', 220, 0),
    ('CS 153', 'Discrete Mathematics', 210, 1),
    ('CS 155', 'Computer Organisation', 200, 2),
    ('CS 251', 'Data Structures and Algorithms', 160, 0),
    ('CS 253', 'Object Oriented Programming', 155, 3),
    ('CS 255', 'Database Systems', 150, 4),
    ('CS 351', 'Operating Systems', 120, 2),
    ('CS 353', 'Computer Networks', 115, 5),
    ('CS 355', 'Software Engineering', 130, 3),
    ('CS 357', 'Theory of Computation', 110, 1),
    ('CS 451', 'Distributed Systems', 85, 6),
    ('CS 453', 'Machine Learning', 95, 6),
    ('CS 455', 'Computer Graphics', 70, 7),
    ('CS 457', 'Information Security', 80, 5),
    ('CS 459', 'Final Year Project', 90, 4),
]

# (group name, course codes) - deliberately overlapping so conflict detection has work to do
GROUPS = [
    ('CS Level 100', ['CS 151', 'CS 153', 'CS 155']),
    ('CS Level 200', ['CS 251', 'CS 253', 'CS 255']),
    ('CS Level 300', ['CS 351', 'CS 353', 'CS 355', 'CS 357']),
    ('CS Level 400', ['CS 451', 'CS 453', 'CS 455', 'CS 457', 'CS 459']),
]

# (student id, name, group name)
STUDENTS = [
    ('20512001', 'Ama Serwaa', 'CS Level 100'),
    ('20512002', 'Kojo Amankwah', 'CS Level 100'),
    ('20412003', 'Abena Frimpong', 'CS Level 200'),
    ('20412004', 'Yaw Boadu', 'CS Level 200'),
    ('20312005', 'Esi Quartey', 'CS Level 300'),
    ('20312006', 'Kwesi Appiah', 'CS Level 300'),
    ('20212007', 'Adjoa Mensimah', 'CS Level 400'),
    ('20212008', 'Nana Yaw Osei', 'CS Level 400'),
]

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']
PERIODS = [
    (time(8, 0), time(10, 0)),
    (time(10, 0), time(12, 0)),
    (time(13, 0), time(15, 0)),
    (time(15, 0), time(17, 0)),
    (time(17, 0), time(19, 0)),
]


class Command(BaseCommand):
    help = 'Seed a realistic KNUST demo dataset. Idempotent - safe to run repeatedly.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--flush',
            action='store_true',
            help='Delete all scheduler data before seeding.',
        )
        parser.add_argument(
            '--tight',
            action='store_true',
            help=(
                'Seed a scarce version of the problem: 3 rooms and 6 time slots. '
                'The generous default is solved perfectly in one generation, which '
                'makes the convergence curve a single point and leaves the genetic '
                'algorithm indistinguishable from greedy first-fit.'
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if options['flush']:
            Notification.objects.all().delete()
            Student.objects.all().delete()
            RescheduleRequest.objects.all().delete()
            TimetableEntry.objects.all().delete()
            StudentGroup.objects.all().delete()
            Course.objects.all().delete()
            Room.objects.all().delete()
            TimeSlot.objects.all().delete()
            Lecturer.objects.all().delete()
            self.stdout.write(self.style.WARNING('Flushed all scheduler data.'))

        tight = options['tight']
        # 3 rooms x 4 slots = 12 room-slot pairs for 15 classes, so a perfect
        # timetable is provably impossible and the search cannot luck into one
        # from its starting population. A merely small problem is not enough:
        # 3 x 6 = 18 pairs still admits a perfect answer and the generator
        # sometimes finds it in generation one, which shows nothing.
        rooms = ROOMS[:3] if tight else ROOMS
        days = DAYS[:2] if tight else DAYS
        periods = PERIODS[:2] if tight else PERIODS

        lecturers = []
        for name, email in LECTURERS:
            lecturer, _ = Lecturer.objects.get_or_create(email=email, defaults={'name': name})
            lecturers.append(lecturer)

        for name, capacity in rooms:
            Room.objects.get_or_create(name=name, defaults={'capacity': capacity})

        courses = {}
        for code, name, expected, lecturer_index in COURSES:
            course, _ = Course.objects.get_or_create(
                code=code,
                defaults={
                    'name': name,
                    'expected_students': expected,
                    'lecturer': lecturers[lecturer_index],
                },
            )
            courses[code] = course

        groups = {}
        for group_name, course_codes in GROUPS:
            group, _ = StudentGroup.objects.get_or_create(name=group_name)
            group.courses.set([courses[c] for c in course_codes])
            groups[group_name] = group

        for student_id, name, group_name in STUDENTS:
            Student.objects.get_or_create(
                student_id=student_id,
                defaults={'name': name, 'group': groups[group_name]},
            )

        for day in days:
            for start, end in periods:
                TimeSlot.objects.get_or_create(day=day, start_time=start, end_time=end)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded: {Lecturer.objects.count()} lecturers, '
            f'{Room.objects.count()} rooms, '
            f'{Course.objects.count()} courses, '
            f'{StudentGroup.objects.count()} groups, '
            f'{Student.objects.count()} students, '
            f'{TimeSlot.objects.count()} time slots.'
        ))
        if tight:
            self.stdout.write(self.style.WARNING(
                'Tight dataset: there are fewer room-slot pairs than classes, so a '
                'perfect timetable is impossible and the generator has to work for '
                'its score. Expect conflicts to remain and fitness to stay below 1.0 '
                '- that is the point, not a failure.'
            ))
