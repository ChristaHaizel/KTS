from datetime import time

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase

from .conflict_detector import detect_conflicts
from .forms import TimeSlotForm
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


class ProposedMoveTests(TestCase):
    """The branch detect_conflicts() takes when validating a reschedule.

    It used to check only the target room, so an approval could introduce
    lecturer and student-group clashes while reporting the move as safe.
    """

    def setUp(self):
        self.lecturer = Lecturer.objects.create(name='Dr. Solo', email='solo@example.com')
        self.c1 = Course.objects.create(
            code='X1', name='One', expected_students=10, lecturer=self.lecturer
        )
        self.c2 = Course.objects.create(
            code='X2', name='Two', expected_students=10, lecturer=self.lecturer
        )
        self.group = StudentGroup.objects.create(name='Level 400')
        self.group.courses.set([self.c1, self.c2])
        self.other_group = StudentGroup.objects.create(name='Level 300')

        self.r1 = Room.objects.create(name='Room 1', capacity=50)
        self.r2 = Room.objects.create(name='Room 2', capacity=50)
        self.slot_a = TimeSlot.objects.create(
            day='MON', start_time=time(8, 0), end_time=time(10, 0)
        )
        self.slot_b = TimeSlot.objects.create(
            day='MON', start_time=time(10, 0), end_time=time(12, 0)
        )
        self.e1 = TimetableEntry.objects.create(
            course=self.c1, room=self.r1, timeslot=self.slot_a, student_group=self.group
        )
        self.e2 = TimetableEntry.objects.create(
            course=self.c2, room=self.r2, timeslot=self.slot_b, student_group=self.group
        )

    def test_detects_lecturer_clash_in_a_free_room(self):
        """Different room, so no room clash - but the lecturer would teach both."""
        found = detect_conflicts(
            exclude_entry=self.e2, check_timeslot=self.slot_a, check_room=self.r2
        )
        self.assertIn('Lecturer Conflict', {c['type'] for c in found})

    def test_detects_group_clash_in_a_free_room(self):
        self.e2.course.lecturer = None  # isolate the group clash from the lecturer one
        self.e2.course.save()
        found = detect_conflicts(
            exclude_entry=self.e2, check_timeslot=self.slot_a, check_room=self.r2
        )
        self.assertIn('Student Group Conflict', {c['type'] for c in found})

    def test_detects_room_clash(self):
        found = detect_conflicts(
            exclude_entry=self.e2, check_timeslot=self.slot_a, check_room=self.r1
        )
        self.assertIn('Room Conflict', {c['type'] for c in found})

    def test_detects_room_too_small_for_the_move(self):
        tiny = Room.objects.create(name='Tiny', capacity=1)
        self.e2.course.lecturer = None
        self.e2.course.save()
        found = detect_conflicts(
            exclude_entry=self.e2, check_timeslot=self.slot_b, check_room=tiny
        )
        self.assertIn('Room Capacity Mismatch', {c['type'] for c in found})

    def test_clean_move_reports_nothing(self):
        free = TimeSlot.objects.create(
            day='TUE', start_time=time(8, 0), end_time=time(10, 0)
        )
        found = detect_conflicts(
            exclude_entry=self.e2, check_timeslot=free, check_room=self.r2
        )
        self.assertEqual(found, [])

    def test_approving_a_clashing_move_is_refused_end_to_end(self):
        """The regression itself: approve used to let this through."""
        admin = make_admin('mover')
        self.client.force_login(admin)
        req = RescheduleRequest.objects.create(
            entry=self.e2, requested_timeslot=self.slot_a,
            requested_room=self.r2, reason='probe', requested_by=admin,
        )
        self.client.post(f'/reschedule/{req.pk}/approve/')

        req.refresh_from_db()
        self.e2.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(self.e2.timeslot, self.slot_b)
        self.assertEqual(detect_conflicts(), [])


class TimeSlotConstraintTests(TestCase):
    def test_end_time_must_be_after_start_time(self):
        slot = TimeSlot(day='MON', start_time=time(10, 0), end_time=time(8, 0))
        with self.assertRaises(DjangoValidationError):
            slot.full_clean()

    def test_equal_start_and_end_is_rejected(self):
        slot = TimeSlot(day='MON', start_time=time(9, 0), end_time=time(9, 0))
        with self.assertRaises(DjangoValidationError):
            slot.full_clean()

    def test_duplicate_slot_is_rejected(self):
        TimeSlot.objects.create(day='MON', start_time=time(8, 0), end_time=time(10, 0))
        duplicate = TimeSlot(day='MON', start_time=time(8, 0), end_time=time(10, 0))
        with self.assertRaises(DjangoValidationError):
            duplicate.full_clean()

    def test_same_period_on_another_day_is_allowed(self):
        TimeSlot.objects.create(day='MON', start_time=time(8, 0), end_time=time(10, 0))
        other = TimeSlot(day='TUE', start_time=time(8, 0), end_time=time(10, 0))
        other.full_clean()  # must not raise
        other.save()
        self.assertEqual(TimeSlot.objects.count(), 2)

    def test_form_surfaces_the_end_before_start_error(self):
        form = TimeSlotForm(data={'day': 'MON', 'start_time': '10:00', 'end_time': '08:00'})
        self.assertFalse(form.is_valid())
        self.assertIn('end_time', form.errors)

    def test_slots_are_ordered_monday_first(self):
        for day in ['FRI', 'MON', 'WED']:
            TimeSlot.objects.create(day=day, start_time=time(8, 0), end_time=time(10, 0))
        self.assertEqual(
            list(TimeSlot.objects.values_list('day', flat=True)),
            ['MON', 'WED', 'FRI'],
        )


class UniqueNameTests(TestCase):
    def test_duplicate_room_name_is_rejected(self):
        Room.objects.create(name='CS Lab 1', capacity=40)
        with self.assertRaises(DjangoValidationError):
            Room(name='CS Lab 1', capacity=60).full_clean()

    def test_duplicate_group_name_is_rejected(self):
        StudentGroup.objects.create(name='CS Level 400')
        with self.assertRaises(DjangoValidationError):
            StudentGroup(name='CS Level 400').full_clean()


class RequestOwnershipTests(TestCase):
    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.entry = TimetableEntry.objects.filter(is_active=True).first()
        self.free_slot = (TimeSlot.objects
                          .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                                   .values_list('timeslot_id', flat=True))
                          .first())

    def test_submitting_records_the_author(self):
        author = make_lecturer_user('requester')
        self.client.force_login(author)
        self.client.post('/reschedule/', {
            'entry': self.entry.pk,
            'timeslot': self.free_slot.pk,
            'room': '',
            'reason': 'Clashes with a departmental meeting.',
        })
        req = RescheduleRequest.objects.get()
        self.assertEqual(req.requested_by, author)
        self.assertEqual(req.status, 'PENDING')

    def test_approval_records_the_decider_and_time(self):
        author = make_lecturer_user('requester2')
        admin = make_admin('decider')
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            requested_room=self.entry.room, reason='swap', requested_by=author,
        )
        self.client.force_login(admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')

        req.refresh_from_db()
        self.assertEqual(req.status, 'APPROVED')
        self.assertEqual(req.requested_by, author)
        self.assertEqual(req.decided_by, admin)
        self.assertIsNotNone(req.decided_at)

    def test_rejection_records_the_decider(self):
        admin = make_admin('decider2')
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot, reason='no',
        )
        self.client.force_login(admin)
        self.client.post(f'/reschedule/{req.pk}/reject/')

        req.refresh_from_db()
        self.assertEqual(req.status, 'REJECTED')
        self.assertEqual(req.decided_by, admin)
        self.assertIsNotNone(req.decided_at)


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
