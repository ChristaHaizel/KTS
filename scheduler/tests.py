from datetime import time

from django.contrib.auth.models import Group, User
from django.test import TestCase

from .conflict_detector import detect_conflicts
from .genetic_algorithm import run_genetic_algorithm
from .models import (
    Course, Lecturer, RescheduleRequest, Room, StudentGroup, TimeSlot, TimetableEntry,
)
from .permissions import ADMIN_GROUP

DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI']
PERIODS = [
    (time(8, 0), time(10, 0)),
    (time(10, 0), time(12, 0)),
    (time(13, 0), time(15, 0)),
    (time(15, 0), time(17, 0)),
]


def build_dataset():
    """Two groups with disjoint enrollment, plus enough rooms and slots to schedule them."""
    lecturers = [
        Lecturer.objects.create(name=f'Lecturer {i}', email=f'l{i}@example.com')
        for i in range(4)
    ]
    courses = [
        Course.objects.create(
            code=f'C{i}', name=f'Course {i}', expected_students=30, lecturer=lecturers[i]
        )
        for i in range(4)
    ]
    group_a = StudentGroup.objects.create(name='Group A')
    group_a.courses.set(courses[:2])
    group_b = StudentGroup.objects.create(name='Group B')
    group_b.courses.set(courses[2:])

    rooms = [Room.objects.create(name=f'Room {i}', capacity=50) for i in range(3)]
    for day in DAYS:
        for start, end in PERIODS:
            TimeSlot.objects.create(day=day, start_time=start, end_time=end)
    return {'courses': courses, 'groups': [group_a, group_b], 'rooms': rooms}


def make_admin(username='admin1'):
    user = User.objects.create_user(username=username, password='pw-for-tests-only')
    user.groups.add(Group.objects.get_or_create(name=ADMIN_GROUP)[0])
    return user


def make_lecturer_user(username='lect1'):
    return User.objects.create_user(username=username, password='pw-for-tests-only')


class GeneticAlgorithmTests(TestCase):
    def setUp(self):
        self.data = build_dataset()

    def test_regeneration_is_idempotent(self):
        """T2.1: Generate must succeed repeatedly, not just the first time."""
        for i in range(5):
            result = run_genetic_algorithm()
            self.assertTrue(result['success'], f'run {i + 1} failed: {result}')

    def test_regeneration_under_tight_supply(self):
        """T2.1: the original failure needed a tight room/slot supply to be deterministic."""
        Room.objects.exclude(pk=self.data['rooms'][0].pk).delete()
        TimeSlot.objects.exclude(
            pk__in=TimeSlot.objects.values_list('pk', flat=True)[:3]
        ).delete()
        for i in range(5):
            result = run_genetic_algorithm()
            self.assertTrue(result['success'], f'tight-supply run {i + 1} failed: {result}')

    def test_groups_only_get_enrolled_courses(self):
        """T2.2: a group must never be scheduled for a course it does not take."""
        run_genetic_algorithm()
        entries = TimetableEntry.objects.filter(is_active=True).select_related(
            'course', 'student_group'
        )
        self.assertGreater(entries.count(), 0)
        for entry in entries:
            self.assertIn(
                entry.course,
                entry.student_group.courses.all(),
                f'{entry.student_group.name} was scheduled for {entry.course.code}, '
                f'which it is not enrolled in',
            )

    def test_schedules_every_enrollment_pair(self):
        """T2.2: a course two groups both take needs two scheduled classes."""
        shared = Course.objects.create(code='SHARED', name='Shared', expected_students=20)
        for group in StudentGroup.objects.all():
            group.courses.add(shared)
        run_genetic_algorithm()
        self.assertEqual(
            TimetableEntry.objects.filter(is_active=True, course=shared).count(), 2
        )

    def test_refuses_when_no_enrollment_exists(self):
        """T2.2: groups with no courses assigned cannot produce a timetable."""
        for group in StudentGroup.objects.all():
            group.courses.clear()
        result = run_genetic_algorithm()
        self.assertFalse(result['success'])
        self.assertIn('Student Group', result['message'])

    def test_reports_dropped_classes(self):
        """T2.5: classes that could not be placed must be reported, not silently dropped."""
        result = run_genetic_algorithm()
        self.assertIn('dropped', result)

    def test_deactivated_entry_releases_its_room_and_slot(self):
        """T2.1: the mechanism behind repeat generation - the uniqueness of
        (room, timeslot) is scoped to active rows, so history does not block a rerun."""
        room = Room.objects.first()
        slot = TimeSlot.objects.first()
        group = StudentGroup.objects.first()
        course = group.courses.first()

        old = TimetableEntry.objects.create(
            course=course, room=room, timeslot=slot, student_group=group, is_active=False
        )
        # Same room+slot must be reusable while the previous row is inactive.
        new = TimetableEntry.objects.create(
            course=course, room=room, timeslot=slot, student_group=group, is_active=True
        )
        self.assertTrue(TimetableEntry.objects.filter(pk=old.pk).exists())
        self.assertTrue(TimetableEntry.objects.filter(pk=new.pk).exists())

    def test_two_active_entries_cannot_share_room_and_slot(self):
        """T2.1: the constraint must still hold for the active timetable."""
        from django.db import IntegrityError, transaction
        room = Room.objects.first()
        slot = TimeSlot.objects.first()
        group = StudentGroup.objects.first()
        course = group.courses.first()

        TimetableEntry.objects.create(
            course=course, room=room, timeslot=slot, student_group=group, is_active=True
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TimetableEntry.objects.create(
                    course=course, room=room, timeslot=slot, student_group=group, is_active=True
                )


class ConflictDetectorTests(TestCase):
    def setUp(self):
        self.lecturer = Lecturer.objects.create(name='Solo', email='solo@example.com')
        self.course_a = Course.objects.create(
            code='A1', name='A', expected_students=10, lecturer=self.lecturer
        )
        self.course_b = Course.objects.create(
            code='B1', name='B', expected_students=10, lecturer=self.lecturer
        )
        self.room = Room.objects.create(name='R1', capacity=50)
        self.other_room = Room.objects.create(name='R2', capacity=50)
        self.slot = TimeSlot.objects.create(
            day='MON', start_time=time(8, 0), end_time=time(10, 0)
        )
        self.group = StudentGroup.objects.create(name='G1')

    def test_clean_timetable_reports_no_conflicts(self):
        TimetableEntry.objects.create(
            course=self.course_a, room=self.room, timeslot=self.slot, student_group=self.group
        )
        self.assertEqual(detect_conflicts(), [])

    def test_flags_lecturer_teaching_two_classes_at_once(self):
        other_group = StudentGroup.objects.create(name='G2')
        TimetableEntry.objects.create(
            course=self.course_a, room=self.room, timeslot=self.slot, student_group=self.group
        )
        TimetableEntry.objects.create(
            course=self.course_b, room=self.other_room, timeslot=self.slot,
            student_group=other_group,
        )
        types = {c['type'] for c in detect_conflicts()}
        self.assertIn('Lecturer Conflict', types)

    def test_flags_group_with_overlapping_classes(self):
        TimetableEntry.objects.create(
            course=self.course_a, room=self.room, timeslot=self.slot, student_group=self.group
        )
        TimetableEntry.objects.create(
            course=self.course_b, room=self.other_room, timeslot=self.slot,
            student_group=self.group,
        )
        types = {c['type'] for c in detect_conflicts()}
        self.assertIn('Student Group Conflict', types)

    def test_flags_room_too_small(self):
        big = Course.objects.create(code='BIG', name='Big', expected_students=500)
        TimetableEntry.objects.create(
            course=big, room=self.room, timeslot=self.slot, student_group=self.group
        )
        types = {c['type'] for c in detect_conflicts()}
        self.assertIn('Room Capacity Mismatch', types)


class TimetableViewTests(TestCase):
    def setUp(self):
        build_dataset()
        self.user = make_admin()
        self.client.force_login(self.user)

    def test_grid_has_one_row_per_period(self):
        """T2.3: 5 days x 4 periods must render 4 rows, not 20."""
        self.assertEqual(TimeSlot.objects.count(), len(DAYS) * len(PERIODS))
        response = self.client.get('/timetable/')
        self.assertEqual(response.status_code, 200)
        grid = response.context['grid']
        self.assertEqual(len(grid), len(PERIODS))
        for row in grid:
            self.assertEqual(len(row['cells']), len(DAYS))

    def test_dashboard_renders_summary_once(self):
        """T2.4: the summary table was rendered twice."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('<table'), 1)


class AccessControlTests(TestCase):
    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.entry = TimetableEntry.objects.filter(is_active=True).first()
        self.slot = TimeSlot.objects.exclude(pk=self.entry.timeslot.pk).first()
        self.request = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.slot, reason='clash'
        )
        self.admin = make_admin()
        self.lecturer_user = make_lecturer_user()

    def test_anonymous_redirected_to_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_get_cannot_reject_reschedule(self):
        """T3.3: state must not change on GET."""
        self.client.force_login(self.admin)
        response = self.client.get(f'/reschedule/{self.request.pk}/reject/')
        self.assertEqual(response.status_code, 405)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, 'PENDING')

    def test_get_cannot_approve_reschedule(self):
        """T3.3: state must not change on GET."""
        self.client.force_login(self.admin)
        response = self.client.get(f'/reschedule/{self.request.pk}/approve/')
        self.assertEqual(response.status_code, 405)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, 'PENDING')

    def test_lecturer_cannot_generate(self):
        """T3.4: only administrators may run the generator."""
        self.client.force_login(self.lecturer_user)
        response = self.client.post('/generate/')
        self.assertIn(response.status_code, (302, 403))

    def test_lecturer_cannot_delete_course(self):
        """T3.4: only administrators may edit reference data."""
        course = Course.objects.first()
        self.client.force_login(self.lecturer_user)
        response = self.client.post(f'/courses/{course.pk}/delete/')
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_admin_can_generate(self):
        self.client.force_login(self.admin)
        response = self.client.post('/generate/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/timetable/', response['Location'])

    def test_lecturer_can_view_timetable(self):
        """Read-only pages stay open to any authenticated user."""
        self.client.force_login(self.lecturer_user)
        self.assertEqual(self.client.get('/timetable/').status_code, 200)
        self.assertEqual(self.client.get('/conflicts/').status_code, 200)


class RescheduleTests(TestCase):
    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.entry = TimetableEntry.objects.filter(is_active=True).first()
        self.admin = make_admin()
        self.client.force_login(self.admin)

    def test_approve_applies_the_move(self):
        free_slot = (TimeSlot.objects
                     .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                              .values_list('timeslot_id', flat=True))
                     .first())
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=free_slot,
            requested_room=self.entry.room, reason='needed',
        )
        response = self.client.post(f'/reschedule/{req.pk}/approve/')
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.entry.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')
        self.assertEqual(self.entry.timeslot, free_slot)

    def test_approve_rejects_conflicting_move(self):
        other = TimetableEntry.objects.filter(is_active=True).exclude(pk=self.entry.pk).first()
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=other.timeslot,
            requested_room=other.room, reason='clash',
        )
        self.client.post(f'/reschedule/{req.pk}/approve/')
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')

    def test_approve_with_no_requested_room_keeps_current_room(self):
        """T3.5: requested_room is nullable and must not blow up on approval."""
        free_slot = (TimeSlot.objects
                     .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                              .values_list('timeslot_id', flat=True))
                     .first())
        original_room = self.entry.room
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=free_slot,
            requested_room=None, reason='room unchanged',
        )
        response = self.client.post(f'/reschedule/{req.pk}/approve/')
        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.entry.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')
        self.assertEqual(self.entry.room, original_room)


class SmokeTests(TestCase):
    """T5.2: walk every route as an admin and assert nothing 500s."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.client.force_login(make_admin())

    def test_all_get_routes_respond(self):
        course = Course.objects.first()
        lecturer = Lecturer.objects.first()
        room = Room.objects.first()
        group = StudentGroup.objects.first()
        slot = TimeSlot.objects.first()
        urls = [
            '/', '/timetable/', '/conflicts/', '/generate/', '/reschedule/',
            '/lecturers/', '/lecturers/add/', f'/lecturers/{lecturer.pk}/edit/',
            f'/lecturers/{lecturer.pk}/delete/',
            '/courses/', '/courses/add/', f'/courses/{course.pk}/edit/',
            f'/courses/{course.pk}/delete/',
            '/rooms/', '/rooms/add/', f'/rooms/{room.pk}/edit/', f'/rooms/{room.pk}/delete/',
            '/student-groups/', '/student-groups/add/',
            f'/student-groups/{group.pk}/edit/', f'/student-groups/{group.pk}/delete/',
            '/timeslots/', '/timeslots/add/', f'/timeslots/{slot.pk}/edit/',
            f'/timeslots/{slot.pk}/delete/',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
