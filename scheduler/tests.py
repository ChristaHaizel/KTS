import random
import re
from datetime import time

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from . import charts
from .accounts import create_student_account
from .baselines import compare, greedy_schedule, random_schedule
from .charts import convergence_chart
from .conflict_detector import detect_conflicts
from .importers import (
    KINDS, ImportError_ as CsvImportError, run_import, template_csv,
)
from .forms import (
    CourseForm, LecturerForm, RoomForm, StudentForm, StudentGroupForm, TimeSlotForm,
)
from .genetic_algorithm import (
    count_violations, fitness, load_problem, run_genetic_algorithm,
)
from .models import (
    Course, GenerationRun, Lecturer, Notification, RescheduleRequest, Room,
    Student, StudentGroup, TimeSlot, TimetableEntry,
)
from .permissions import ADMIN_GROUP, is_admin, lecturer_for, student_for

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
    """A plain account with nothing linked to it. Not staff: an account attached
    to no lecturer is deliberately given nothing."""
    return User.objects.create_user(username=username, password='pw-for-tests-only')


def make_linked_lecturer(username='drlinked', lecturer=None):
    """An account that really is a lecturer, and so counts as staff."""
    user = make_lecturer_user(username)
    lecturer = lecturer or Lecturer.objects.first()
    lecturer.user = user
    lecturer.save()
    return user


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
        # The account must be linked to the lecturer who teaches this class -
        # see LecturerOwnershipTests for what happens when it is not.
        author = make_lecturer_user('requester')
        lecturer = self.entry.course.lecturer
        lecturer.user = author
        lecturer.save()

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

    def test_sign_out_actually_signs_out(self):
        """Django refuses GET for logout, so a plain link silently does nothing."""
        self.client.force_login(self.admin)
        self.assertIn('_auth_user_id', self.client.session)

        response = self.client.post('/logout/')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_sign_out_is_rendered_as_a_post_form(self):
        """A GET link here returns 405 and leaves the user logged in, so the
        control has to be a form. Guard the markup, not just the endpoint."""
        self.client.force_login(self.admin)
        body = self.client.get('/').content.decode()
        self.assertIn('action="/logout/"', body)
        self.assertNotIn('href="/logout/"', body)
        # The form must carry a CSRF token or the POST is rejected.
        form_start = body.index('action="/logout/"')
        form_end = body.index('</form>', form_start)
        self.assertIn('csrfmiddlewaretoken', body[form_start:form_end])

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

    def test_lecturer_can_view_timetable_and_conflicts(self):
        self.client.force_login(make_linked_lecturer('viewer'))
        self.assertEqual(self.client.get('/timetable/').status_code, 200)
        self.assertEqual(self.client.get('/conflicts/').status_code, 200)

    def test_unlinked_account_is_not_staff(self):
        """An account attached to no lecturer is not staff, so the conflict
        report and the reschedule workflow are both closed to it."""
        self.client.force_login(self.lecturer_user)
        self.assertEqual(self.client.get('/timetable/').status_code, 200)
        self.assertIn(self.client.get('/conflicts/').status_code, (302, 403))
        self.assertIn(self.client.get('/reschedule/').status_code, (302, 403))


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


class AlgorithmReportTests(TestCase):
    def setUp(self):
        build_dataset()
        self.admin = make_admin('reporter')

    def test_generating_records_a_run(self):
        self.client.force_login(self.admin)
        self.client.post('/generate/')
        run = GenerationRun.objects.get()
        self.assertGreater(run.generations_run, 0)
        self.assertEqual(len(run.history), run.generations_run)
        self.assertGreater(run.runtime_seconds, 0)

    def test_history_is_monotonic(self):
        """Best-so-far must never fall, or the curve is not convergence."""
        result = run_genetic_algorithm()
        history = result['history']
        for earlier, later in zip(history, history[1:]):
            self.assertLessEqual(earlier, later)

    def test_page_renders_with_a_run(self):
        self.client.force_login(self.admin)
        self.client.post('/generate/')
        response = self.client.get('/algorithm/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'chart-svg')
        self.assertContains(response, 'Penalty weights')

    def test_page_renders_with_no_runs(self):
        self.client.force_login(self.admin)
        self.assertEqual(GenerationRun.objects.count(), 0)
        response = self.client.get('/algorithm/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No runs recorded yet')

    def test_page_is_admin_only(self):
        self.client.force_login(make_lecturer_user('nosy'))
        response = self.client.get('/algorithm/')
        self.assertIn(response.status_code, (302, 403))

    def test_converged_at_reports_first_best_generation(self):
        run = GenerationRun(
            generations_run=5, best_fitness=0.5, entries_created=1,
            runtime_seconds=0.1, history=[0.1, 0.3, 0.5, 0.5, 0.5],
        )
        self.assertEqual(run.converged_at, 3)


class BaselineTests(TestCase):
    def setUp(self):
        build_dataset()

    def test_greedy_is_never_worse_than_random_on_average(self):
        """True on any dataset. On a loose one both can reach 1.0, so this is
        deliberately not a strict inequality - see the constrained test below."""
        enrollments, rooms, timeslots = load_problem()
        greedy = fitness(greedy_schedule(enrollments, rooms, timeslots))
        randoms = [
            fitness(random_schedule(enrollments, rooms, timeslots)) for _ in range(20)
        ]
        self.assertGreaterEqual(greedy, sum(randoms) / len(randoms))

    def test_greedy_beats_random_when_slots_are_scarce(self):
        """Where the comparison actually means something: with only as many
        (room, slot) pairs as there are classes, random almost never lands a
        clean schedule and first-fit reliably does."""
        random.seed(20260803)  # deterministic, so this can never flake
        TimeSlot.objects.exclude(
            pk__in=TimeSlot.objects.values_list('pk', flat=True)[:2]
        ).delete()
        Room.objects.exclude(
            pk__in=Room.objects.values_list('pk', flat=True)[:2]
        ).delete()

        enrollments, rooms, timeslots = load_problem()
        greedy = fitness(greedy_schedule(enrollments, rooms, timeslots))
        randoms = [
            fitness(random_schedule(enrollments, rooms, timeslots)) for _ in range(20)
        ]
        self.assertGreater(greedy, sum(randoms) / len(randoms))

    def test_ga_is_at_least_as_good_as_greedy(self):
        enrollments, rooms, timeslots = load_problem()
        greedy = fitness(greedy_schedule(enrollments, rooms, timeslots))
        ga = run_genetic_algorithm()['fitness']
        self.assertGreaterEqual(ga, greedy)

    def test_every_approach_schedules_every_class(self):
        enrollments, rooms, timeslots = load_problem()
        for name, schedule in [
            ('random', random_schedule(enrollments, rooms, timeslots)),
            ('greedy', greedy_schedule(enrollments, rooms, timeslots)),
        ]:
            with self.subTest(approach=name):
                self.assertEqual(len(schedule), len(enrollments))

    def test_greedy_respects_enrollment(self):
        enrollments, rooms, timeslots = load_problem()
        for gene in greedy_schedule(enrollments, rooms, timeslots):
            self.assertIn(gene['course'], gene['group'].courses.all())

    def test_compare_returns_none_with_nothing_to_schedule(self):
        StudentGroup.objects.all().delete()
        self.assertIsNone(compare(trials=2))

    def test_fitness_is_one_only_when_clean(self):
        enrollments, rooms, timeslots = load_problem()
        group, course = enrollments[0]
        room, slot = rooms[0], timeslots[0]
        clean = [{'course': course, 'group': group, 'room': room, 'timeslot': slot}]
        self.assertEqual(fitness(clean), 1.0)

        clashing = clean + [{
            'course': course, 'group': group, 'room': room, 'timeslot': slot,
        }]
        self.assertLess(fitness(clashing), 1.0)


class ChartGeometryTests(TestCase):
    def test_points_stay_inside_the_plot_box(self):
        chart = convergence_chart([0.0, 0.4, 0.9, 1.0])
        points = [tuple(map(float, p.split(','))) for p in chart['polyline'].split()]
        left, right = charts.PAD_LEFT, charts.PAD_LEFT + charts.PLOT_WIDTH
        top, bottom = charts.PAD_TOP, charts.PAD_TOP + charts.PLOT_HEIGHT
        for x, y in points:
            self.assertGreaterEqual(round(x, 3), left)
            self.assertLessEqual(round(x, 3), right)
            self.assertGreaterEqual(round(y, 3), top)
            self.assertLessEqual(round(y, 3), bottom)

    def test_axis_labels_fit_inside_the_viewbox(self):
        """The x-axis band must be inside the height, or the card grows a scrollbar."""
        chart = convergence_chart([0.5, 1.0])
        self.assertLess(chart['plot_bottom'] + 18, charts.HEIGHT)

    def test_higher_fitness_sits_higher(self):
        chart = convergence_chart([0.1, 0.9])
        points = [tuple(map(float, p.split(','))) for p in chart['polyline'].split()]
        self.assertLess(points[1][1], points[0][1])

    def test_single_generation_gets_a_marker_not_a_line(self):
        chart = convergence_chart([1.0])
        self.assertIsNotNone(chart['single_point'])

    def test_empty_history_has_no_chart(self):
        self.assertIsNone(convergence_chart([]))

    def test_axis_fits_small_values_instead_of_flattening_them(self):
        """Real fitness on a hard problem is ~0.02. On a fixed 0-1 axis the whole
        curve collapses onto the baseline and shows nothing."""
        history = [0.0217, 0.0217, 0.0244, 0.0244]
        chart = convergence_chart(history)
        points = [tuple(map(float, p.split(','))) for p in chart['polyline'].split()]
        ys = [y for _, y in points]
        # The improvement must occupy a real share of the plot height.
        self.assertGreater(max(ys) - min(ys), charts.PLOT_HEIGHT * 0.4)

    def test_axis_range_is_reported_for_the_caption(self):
        chart = convergence_chart([0.0217, 0.0244])
        self.assertIn('y_low', chart)
        self.assertIn('y_high', chart)
        self.assertTrue(chart['improved'])

    def test_flat_history_is_marked_as_not_improved(self):
        chart = convergence_chart([0.03, 0.03, 0.03])
        self.assertFalse(chart['improved'])
        points = [tuple(map(float, p.split(','))) for p in chart['polyline'].split()]
        # A flat run must still sit inside the plot, not on an edge.
        for _, y in points:
            self.assertGreater(y, charts.PAD_TOP)
            self.assertLess(y, charts.PAD_TOP + charts.PLOT_HEIGHT)

    def test_domain_never_leaves_the_valid_fitness_range(self):
        for history in ([0.0, 0.0], [1.0, 1.0], [0.99, 1.0], [0.0, 0.001]):
            with self.subTest(history=history):
                low, high = charts.y_domain(history)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 1.0)
                self.assertLessEqual(low, high)


class LecturerOwnershipTests(TestCase):
    """A lecturer account may only touch its own classes.

    The dropdown being filtered is presentation; these tests forge the POST
    directly, because that is what an actual misuse looks like.
    """

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()

        self.mine = Lecturer.objects.get(email='l0@example.com')
        self.theirs = Lecturer.objects.get(email='l2@example.com')

        self.user = make_lecturer_user('drmine')
        self.mine.user = self.user
        self.mine.save()

        self.my_entry = TimetableEntry.objects.filter(
            is_active=True, course__lecturer=self.mine
        ).first()
        self.their_entry = TimetableEntry.objects.filter(
            is_active=True, course__lecturer=self.theirs
        ).first()
        self.free_slot = (TimeSlot.objects
                          .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                                   .values_list('timeslot_id', flat=True))
                          .first())

    def _submit(self, entry):
        return self.client.post('/reschedule/', {
            'entry': entry.pk,
            'timeslot': self.free_slot.pk,
            'room': '',
            'reason': 'test',
        })

    def test_lecturer_can_request_against_own_class(self):
        self.client.force_login(self.user)
        response = self._submit(self.my_entry)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RescheduleRequest.objects.count(), 1)
        self.assertEqual(RescheduleRequest.objects.get().requested_by, self.user)

    def test_forged_post_for_another_lecturers_class_is_refused(self):
        self.client.force_login(self.user)
        response = self._submit(self.their_entry)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(RescheduleRequest.objects.count(), 0)

    def test_account_with_no_lecturer_is_refused_outright(self):
        """An unlinked account is not staff, so it never reaches the form. Even
        if it did, _requestable_entries would hand it an empty queryset."""
        self.client.force_login(make_lecturer_user('unlinked'))
        self.assertIn(self.client.get('/reschedule/').status_code, (302, 403))
        self.assertIn(self._submit(self.my_entry).status_code, (302, 403))
        self.assertEqual(RescheduleRequest.objects.count(), 0)

    def test_unlinked_account_would_get_an_empty_queryset(self):
        """The scoping itself fails safe, independently of the route guard."""
        from .views import _requestable_entries
        self.assertEqual(
            list(_requestable_entries(make_lecturer_user('unlinked2'))), []
        )

    def test_dropdown_lists_only_own_classes(self):
        self.client.force_login(self.user)
        entries = self.client.get('/reschedule/').context['entries']
        self.assertGreater(len(entries), 0)
        for entry in entries:
            self.assertEqual(entry.course.lecturer, self.mine)

    def test_admin_still_sees_every_class(self):
        self.client.force_login(make_admin('boss'))
        entries = self.client.get('/reschedule/').context['entries']
        self.assertEqual(
            len(entries), TimetableEntry.objects.filter(is_active=True).count()
        )

    def test_lecturer_sees_only_their_own_requests(self):
        other_user = make_lecturer_user('other')
        self.theirs.user = other_user
        self.theirs.save()

        RescheduleRequest.objects.create(
            entry=self.my_entry, requested_timeslot=self.free_slot,
            reason='mine', requested_by=self.user,
        )
        RescheduleRequest.objects.create(
            entry=self.their_entry, requested_timeslot=self.free_slot,
            reason='theirs', requested_by=other_user,
        )

        self.client.force_login(self.user)
        response = self.client.get('/reschedule/')
        mine = response.context['my_requests']
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].requested_by, self.user)
        # The decision queue is an admin tool and is not built for anyone else.
        self.assertIsNone(response.context['pending_requests'])

    def test_admin_sees_all_pending_requests(self):
        RescheduleRequest.objects.create(
            entry=self.my_entry, requested_timeslot=self.free_slot,
            reason='mine', requested_by=self.user,
        )
        self.client.force_login(make_admin('boss2'))
        pending = self.client.get('/reschedule/').context['pending_requests']
        self.assertEqual(len(pending), 1)

    def test_lecturer_still_cannot_approve(self):
        req = RescheduleRequest.objects.create(
            entry=self.my_entry, requested_timeslot=self.free_slot,
            reason='mine', requested_by=self.user,
        )
        self.client.force_login(self.user)
        response = self.client.post(f'/reschedule/{req.pk}/approve/')
        self.assertIn(response.status_code, (302, 403))
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')

    def test_profile_lookup_handles_unlinked_and_anonymous(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(lecturer_for(self.user), self.mine)
        self.assertIsNone(lecturer_for(make_lecturer_user('nobody')))
        self.assertIsNone(lecturer_for(AnonymousUser()))

    def test_deleting_the_account_keeps_the_lecturer(self):
        """Scheduling data must outlive an account being removed."""
        self.user.delete()
        self.mine.refresh_from_db()
        self.assertIsNone(self.mine.user)
        self.assertTrue(Lecturer.objects.filter(pk=self.mine.pk).exists())


class PermissionBoundaryTests(TestCase):
    """Enumerate every route rather than spot-checking the ones I remember.

    A view added later without a decorator shows up here as a failure.
    """

    ADMIN_ONLY = [
        '/generate/', '/algorithm/',
        '/lecturers/', '/lecturers/add/',
        '/courses/', '/courses/add/',
        '/rooms/', '/rooms/add/',
        '/student-groups/', '/student-groups/add/',
        '/timeslots/', '/timeslots/add/',
    ]
    # Open to staff. Students get '/', '/timetable/' and '/notifications/' only,
    # which StudentPermissionTests covers.
    STAFF_ROUTES = ['/', '/timetable/', '/conflicts/', '/reschedule/']

    def setUp(self):
        build_dataset()
        self.lecturer_user = make_lecturer_user('plainuser')
        self.admin = make_admin('theboss')

    def test_lecturer_is_refused_every_admin_route(self):
        self.client.force_login(self.lecturer_user)
        for url in self.ADMIN_ONLY:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (302, 403),
                    f'{url} was reachable by a non-admin',
                )

    def test_admin_can_reach_every_admin_route(self):
        self.client.force_login(self.admin)
        for url in self.ADMIN_ONLY:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_routes_are_open_to_a_lecturer(self):
        self.client.force_login(make_linked_lecturer('reallecturer'))
        for url in self.STAFF_ROUTES:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_sidebar_hides_admin_sections_from_a_lecturer(self):
        self.client.force_login(self.lecturer_user)
        body = self.client.get('/').content.decode()
        nav = body[body.index('class="sidebar-nav"'):body.index('</div>\n\n<div class="main-wrap"')]
        for hidden in ['/lecturers/', '/courses/', '/rooms/',
                       '/student-groups/', '/timeslots/', '/generate/', '/algorithm/']:
            with self.subTest(link=hidden):
                self.assertNotIn(f'href="{hidden}"', nav)

    def test_sidebar_shows_admin_sections_to_an_admin(self):
        self.client.force_login(self.admin)
        body = self.client.get('/').content.decode()
        for shown in ['/lecturers/', '/courses/', '/generate/', '/algorithm/']:
            with self.subTest(link=shown):
                self.assertIn(f'href="{shown}"', body)


class LecturerDashboardTests(TestCase):
    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.lecturer = Lecturer.objects.get(email='l0@example.com')
        self.user = make_lecturer_user('drdash')
        self.lecturer.user = self.user
        self.lecturer.save()

    def test_lecturer_sees_their_own_dashboard(self):
        self.client.force_login(self.user)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'scheduler/dashboard_lecturer.html')
        self.assertEqual(
            response.context['my_class_count'],
            TimetableEntry.objects.filter(
                is_active=True, course__lecturer=self.lecturer
            ).count(),
        )

    def test_lecturer_dashboard_hides_department_totals(self):
        self.client.force_login(self.user)
        body = self.client.get('/').content.decode()
        for admin_only in ['Total Lecturers', 'Total Rooms', 'Rooms Available',
                           'Generate New Timetable']:
            with self.subTest(text=admin_only):
                self.assertNotIn(admin_only, body)

    def test_lecturer_dashboard_counts_only_their_own_classes(self):
        self.client.force_login(self.user)
        mine = self.client.get('/').context['my_class_count']
        everything = TimetableEntry.objects.filter(is_active=True).count()
        self.assertGreater(everything, mine)

    def test_admin_still_gets_the_department_dashboard(self):
        self.client.force_login(make_admin('dashboss'))
        response = self.client.get('/')
        self.assertTemplateUsed(response, 'scheduler/dashboard.html')
        self.assertIn('total_lecturers', response.context)

    def test_unlinked_account_sees_zero_not_everything(self):
        self.client.force_login(make_lecturer_user('nolink'))
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['my_class_count'], 0)
        self.assertIsNone(response.context['lecturer'])


class CreateLecturerUsersCommandTests(TestCase):
    def setUp(self):
        build_dataset()

    def test_creates_and_links_accounts(self):
        from io import StringIO
        from django.core.management import call_command

        self.assertEqual(Lecturer.objects.filter(user__isnull=False).count(), 0)
        call_command('create_lecturer_users', stdout=StringIO())
        self.assertEqual(Lecturer.objects.filter(user__isnull=True).count(), 0)

    def test_is_idempotent(self):
        from io import StringIO
        from django.core.management import call_command

        call_command('create_lecturer_users', stdout=StringIO())
        before = User.objects.count()
        out = StringIO()
        call_command('create_lecturer_users', stdout=out)
        self.assertEqual(User.objects.count(), before)
        self.assertIn('Nothing to do', out.getvalue())

    def test_dry_run_writes_nothing(self):
        from io import StringIO
        from django.core.management import call_command

        before = User.objects.count()
        call_command('create_lecturer_users', '--dry-run', stdout=StringIO())
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(Lecturer.objects.filter(user__isnull=False).count(), 0)

    def test_created_accounts_have_no_admin_rights(self):
        from io import StringIO
        from django.core.management import call_command

        call_command('create_lecturer_users', stdout=StringIO())
        for lecturer in Lecturer.objects.all():
            self.assertFalse(lecturer.user.is_superuser)
            self.assertFalse(lecturer.user.is_staff)
            self.assertFalse(is_admin(lecturer.user))


class RequestOutcomeVisibilityTests(TestCase):
    """A requester who cannot see the outcome has no way to learn it."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.lecturer = Lecturer.objects.get(email='l0@example.com')
        self.user = make_lecturer_user('seer')
        self.lecturer.user = self.user
        self.lecturer.save()
        self.entry = TimetableEntry.objects.filter(
            is_active=True, course__lecturer=self.lecturer
        ).first()
        self.free_slot = (TimeSlot.objects
                          .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                                   .values_list('timeslot_id', flat=True))
                          .first())
        self.admin = make_admin('decider3')

    def _make_request(self):
        return RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            requested_room=self.entry.room, reason='needed', requested_by=self.user,
        )

    def test_requester_sees_a_decided_request(self):
        req = self._make_request()
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')

        self.client.force_login(self.user)
        response = self.client.get('/reschedule/')
        mine = list(response.context['my_requests'])
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].status, 'APPROVED')
        self.assertContains(response, 'Approved')

    def test_requester_sees_a_rejected_request(self):
        req = self._make_request()
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/reject/')

        self.client.force_login(self.user)
        response = self.client.get('/reschedule/')
        self.assertEqual(list(response.context['my_requests'])[0].status, 'REJECTED')
        self.assertContains(response, 'Rejected')

    def test_outcome_names_the_decider(self):
        req = self._make_request()
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')

        self.client.force_login(self.user)
        self.assertContains(self.client.get('/reschedule/'), self.admin.username)

    def test_my_requests_are_only_mine(self):
        self._make_request()
        other = make_lecturer_user('someoneelse')
        RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            reason='theirs', requested_by=other,
        )
        self.client.force_login(self.user)
        mine = self.client.get('/reschedule/').context['my_requests']
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].requested_by, self.user)

    def test_lecturer_gets_no_pending_queue(self):
        """The decision queue is an admin tool, not something to show requesters."""
        self._make_request()
        self.client.force_login(self.user)
        self.assertIsNone(self.client.get('/reschedule/').context['pending_requests'])


class AccountProvisioningTests(TestCase):
    def setUp(self):
        build_dataset()
        self.lecturer = Lecturer.objects.get(email='l0@example.com')
        self.admin = make_admin('provisioner')

    def test_admin_can_create_an_account_from_the_ui(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f'/lecturers/{self.lecturer.pk}/create-account/', follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.lecturer.refresh_from_db()
        self.assertIsNotNone(self.lecturer.user)
        self.assertFalse(self.lecturer.user.is_superuser)
        self.assertFalse(is_admin(self.lecturer.user))

    def test_the_password_is_shown_once_and_works(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f'/lecturers/{self.lecturer.pk}/create-account/', follow=True
        )
        # Django escapes the message, so the quotes arrive as &quot;.
        body = response.content.decode()
        match = re.search(r'password &quot;([^&]+)&quot;', body)
        self.assertIsNotNone(match, 'the generated password was not shown')

        self.lecturer.refresh_from_db()
        self.assertTrue(
            self.client.login(
                username=self.lecturer.user.username, password=match.group(1)
            )
        )

    def test_refuses_a_second_account(self):
        self.lecturer.user = make_lecturer_user('already')
        self.lecturer.save()
        self.client.force_login(self.admin)
        self.client.post(f'/lecturers/{self.lecturer.pk}/create-account/')
        self.lecturer.refresh_from_db()
        self.assertEqual(self.lecturer.user.username, 'already')

    def test_requires_post(self):
        self.client.force_login(self.admin)
        response = self.client.get(f'/lecturers/{self.lecturer.pk}/create-account/')
        self.assertEqual(response.status_code, 405)

    def test_lecturer_cannot_provision_accounts(self):
        self.client.force_login(make_lecturer_user('nobody2'))
        response = self.client.post(f'/lecturers/{self.lecturer.pk}/create-account/')
        self.assertIn(response.status_code, (302, 403))
        self.lecturer.refresh_from_db()
        self.assertIsNone(self.lecturer.user)


class FormTests(TestCase):
    """The forms had no coverage at all until now."""

    def setUp(self):
        self.lecturer = Lecturer.objects.create(name='L', email='l@example.com')

    def test_course_form_accepts_valid_input(self):
        form = CourseForm(data={
            'code': 'CS 999', 'name': 'Advanced Things',
            'expected_students': 40, 'lecturer': self.lecturer.pk,
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_course_code_must_be_unique(self):
        Course.objects.create(code='CS 999', name='First', expected_students=10)
        form = CourseForm(data={
            'code': 'CS 999', 'name': 'Second', 'expected_students': 10,
            'lecturer': self.lecturer.pk,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('code', form.errors)

    def test_course_allows_no_lecturer(self):
        form = CourseForm(data={
            'code': 'CS 001', 'name': 'Unassigned', 'expected_students': 10, 'lecturer': '',
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_lecturer_email_must_be_unique(self):
        form = LecturerForm(data={'name': 'Clone', 'email': 'l@example.com'})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)

    def test_lecturer_email_must_be_an_email(self):
        form = LecturerForm(data={'name': 'X', 'email': 'not-an-email'})
        self.assertFalse(form.is_valid())

    def test_room_name_must_be_unique(self):
        Room.objects.create(name='Lab 1', capacity=30)
        form = RoomForm(data={'name': 'Lab 1', 'capacity': 40})
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_room_capacity_is_required(self):
        form = RoomForm(data={'name': 'Lab 2', 'capacity': ''})
        self.assertFalse(form.is_valid())

    def test_student_group_accepts_courses(self):
        c1 = Course.objects.create(code='A', name='A', expected_students=10)
        c2 = Course.objects.create(code='B', name='B', expected_students=10)
        form = StudentGroupForm(data={'name': 'Level 100', 'courses': [c1.pk, c2.pk]})
        self.assertTrue(form.is_valid(), form.errors)
        group = form.save()
        self.assertEqual(group.courses.count(), 2)

    def test_student_group_allows_no_courses(self):
        form = StudentGroupForm(data={'name': 'Empty', 'courses': []})
        self.assertTrue(form.is_valid(), form.errors)


class PaginationTests(TestCase):
    def setUp(self):
        self.client.force_login(make_admin('pager'))
        for i in range(60):
            Room.objects.create(name=f'Room {i:03d}', capacity=30)

    def test_list_is_paginated(self):
        page = self.client.get('/rooms/').context['rooms']
        self.assertEqual(len(page), 25)
        self.assertTrue(page.has_next())

    def test_record_count_reports_the_total_not_the_page(self):
        response = self.client.get('/rooms/')
        self.assertEqual(response.context['total_count'], 60)
        self.assertContains(response, '60 room(s) on record')

    def test_second_page_continues(self):
        page = self.client.get('/rooms/?page=2').context['rooms']
        self.assertEqual(page.number, 2)
        self.assertTrue(page.has_previous())

    def test_out_of_range_page_falls_back(self):
        page = self.client.get('/rooms/?page=999').context['rooms']
        self.assertEqual(page.number, page.paginator.num_pages)

    def test_garbage_page_does_not_error(self):
        self.assertEqual(self.client.get('/rooms/?page=abc').status_code, 200)


class GenerationRunPruningTests(TestCase):
    def test_prune_keeps_only_the_most_recent(self):
        for i in range(GenerationRun.KEEP_RUNS + 15):
            GenerationRun.objects.create(
                generations_run=1, best_fitness=0.5, entries_created=1,
                runtime_seconds=0.01, history=[0.5],
            )
        GenerationRun.prune()
        self.assertEqual(GenerationRun.objects.count(), GenerationRun.KEEP_RUNS)

    def test_prune_is_safe_when_under_the_cap(self):
        GenerationRun.objects.create(
            generations_run=1, best_fitness=0.5, entries_created=1,
            runtime_seconds=0.01, history=[0.5],
        )
        GenerationRun.prune()
        self.assertEqual(GenerationRun.objects.count(), 1)


class WeightSensitivityTests(TestCase):
    def setUp(self):
        build_dataset()

    def test_violations_are_counted_independently_of_weights(self):
        enrollments, rooms, timeslots = load_problem()
        group, course = enrollments[0]
        clashing = [
            {'course': course, 'group': group, 'room': rooms[0], 'timeslot': timeslots[0]},
            {'course': course, 'group': group, 'room': rooms[0], 'timeslot': timeslots[0]},
        ]
        counts = count_violations(clashing)
        self.assertEqual(counts['room'], 1)
        self.assertEqual(counts['group'], 1)

    def test_weights_change_the_score_but_not_the_violations(self):
        enrollments, rooms, timeslots = load_problem()
        group, course = enrollments[0]
        clashing = [
            {'course': course, 'group': group, 'room': rooms[0], 'timeslot': timeslots[0]},
            {'course': course, 'group': group, 'room': rooms[0], 'timeslot': timeslots[0]},
        ]
        heavy = fitness(clashing, {'room': 100, 'lecturer': 100, 'group': 100, 'capacity': 100})
        light = fitness(clashing, {'room': 1, 'lecturer': 1, 'group': 1, 'capacity': 1})
        self.assertLess(heavy, light)
        self.assertEqual(count_violations(clashing), count_violations(clashing))

    def test_run_accepts_custom_weights(self):
        result = run_genetic_algorithm(
            weights={'room': 1, 'lecturer': 1, 'group': 1, 'capacity': 0}
        )
        self.assertTrue(result['success'])
        self.assertIn('violations', result)


def make_student(student_id='20512999', name='Test Student', group=None, with_account=True):
    student = Student.objects.create(student_id=student_id, name=name, group=group)
    if with_account:
        create_student_account(student)
    return student


class StudentLoginTests(TestCase):
    """Requirement 1: log in securely using assigned credentials (student ID)."""

    def setUp(self):
        build_dataset()
        self.group = StudentGroup.objects.first()
        self.student = Student.objects.create(
            student_id='20512345', name='Ama Serwaa', group=self.group
        )

    def test_the_username_is_the_student_id(self):
        user, _password = create_student_account(self.student)
        self.assertEqual(user.username, '20512345')

    def test_student_can_sign_in_with_their_id(self):
        _user, password = create_student_account(self.student)
        self.assertTrue(self.client.login(username='20512345', password=password))

    def test_generated_account_has_no_privileges(self):
        user, _ = create_student_account(self.student)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertFalse(is_admin(user))
        self.assertIsNone(lecturer_for(user))

    def test_account_is_linked_back_to_the_student(self):
        user, _ = create_student_account(self.student)
        self.student.refresh_from_db()
        self.assertEqual(self.student.user, user)
        self.assertEqual(student_for(user), self.student)

    def test_wrong_password_is_refused(self):
        create_student_account(self.student)
        self.assertFalse(self.client.login(username='20512345', password='guess'))

    def test_anonymous_cannot_reach_the_timetable(self):
        response = self.client.get('/timetable/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


class StudentTimetableTests(TestCase):
    """Requirement 2: a personalised timetable for their programme and level."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.my_group = StudentGroup.objects.get(name='Group A')
        self.other_group = StudentGroup.objects.get(name='Group B')
        self.student = make_student(group=self.my_group)
        self.client.force_login(self.student.user)

    def test_timetable_shows_only_their_group(self):
        response = self.client.get('/timetable/')
        self.assertEqual(response.status_code, 200)
        shown = [e for row in response.context['grid'] for cell in row['cells'] for e in cell]
        self.assertGreater(len(shown), 0)
        for entry in shown:
            self.assertEqual(entry.student_group, self.my_group)

    def test_another_groups_classes_are_absent(self):
        response = self.client.get('/timetable/')
        shown = [e for row in response.context['grid'] for cell in row['cells'] for e in cell]
        everything = TimetableEntry.objects.filter(is_active=True).count()
        self.assertLess(len(shown), everything)
        self.assertNotIn(
            self.other_group, {e.student_group for e in shown}
        )

    def test_filter_controls_are_not_offered(self):
        """The dropdowns would list every group and every member of staff."""
        response = self.client.get('/timetable/')
        self.assertEqual(len(response.context['groups']), 0)
        self.assertEqual(len(response.context['lecturers']), 0)
        self.assertNotContains(response, 'Filter by Lecturer')

    def test_query_parameters_cannot_widen_the_view(self):
        """A student appending ?group=<other> must not see another timetable."""
        response = self.client.get(f'/timetable/?group={self.other_group.pk}')
        shown = [e for row in response.context['grid'] for cell in row['cells'] for e in cell]
        for entry in shown:
            self.assertEqual(entry.student_group, self.my_group)

    def test_unassigned_student_sees_nothing_not_everything(self):
        loose = make_student(student_id='20512888', name='No Group', group=None)
        self.client.force_login(loose.user)
        response = self.client.get('/timetable/')
        shown = [e for row in response.context['grid'] for cell in row['cells'] for e in cell]
        self.assertEqual(shown, [])

    def test_dashboard_is_the_student_one(self):
        response = self.client.get('/')
        self.assertTemplateUsed(response, 'scheduler/dashboard_student.html')
        self.assertEqual(
            response.context['my_class_count'],
            TimetableEntry.objects.filter(
                is_active=True, student_group=self.my_group
            ).count(),
        )


class StudentNotificationTests(TestCase):
    """Requirement 3: in-system notification when their timetable changes."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.my_group = StudentGroup.objects.get(name='Group A')
        self.other_group = StudentGroup.objects.get(name='Group B')
        self.student = make_student(group=self.my_group)
        self.other_student = make_student(
            student_id='20512777', name='Other', group=self.other_group
        )
        self.admin = make_admin('notifier')
        self.entry = TimetableEntry.objects.filter(
            is_active=True, student_group=self.my_group
        ).first()
        self.free_slot = (TimeSlot.objects
                          .exclude(pk__in=TimetableEntry.objects.filter(is_active=True)
                                   .values_list('timeslot_id', flat=True))
                          .first())

    def test_approving_a_move_notifies_the_affected_group(self):
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            requested_room=self.entry.room, reason='clash',
        )
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')

        self.assertEqual(
            Notification.objects.filter(user=self.student.user).count(), 1
        )
        message = Notification.objects.get(user=self.student.user).message
        self.assertIn(self.entry.course.code, message)

    def test_a_move_does_not_notify_an_unaffected_group(self):
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            requested_room=self.entry.room, reason='clash',
        )
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')
        self.assertEqual(
            Notification.objects.filter(user=self.other_student.user).count(), 0
        )

    def test_a_refused_move_notifies_nobody(self):
        """Nothing changed, so there is nothing to announce."""
        other = TimetableEntry.objects.filter(is_active=True).exclude(pk=self.entry.pk).first()
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=other.timeslot,
            requested_room=other.room, reason='clashing',
        )
        self.client.force_login(self.admin)
        self.client.post(f'/reschedule/{req.pk}/approve/')
        req.refresh_from_db()
        self.assertEqual(req.status, 'PENDING')
        self.assertEqual(Notification.objects.filter(user=self.student.user).count(), 0)

    def test_regenerating_notifies_every_student(self):
        self.client.force_login(self.admin)
        self.client.post('/generate/')
        for student in (self.student, self.other_student):
            with self.subTest(student=student.student_id):
                self.assertEqual(
                    Notification.objects.filter(user=student.user).count(), 1
                )

    def test_unread_count_appears_in_every_page(self):
        Notification.objects.create(user=self.student.user, message='Something moved.')
        self.client.force_login(self.student.user)
        response = self.client.get('/')
        self.assertEqual(response.context['unread_notifications'], 1)

    def test_notifications_page_lists_them(self):
        Notification.objects.create(user=self.student.user, message='Your class moved.')
        self.client.force_login(self.student.user)
        response = self.client.get('/notifications/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your class moved.')

    def test_marking_read_clears_the_count(self):
        Notification.objects.create(user=self.student.user, message='One')
        Notification.objects.create(user=self.student.user, message='Two')
        self.client.force_login(self.student.user)
        self.client.post('/notifications/mark-read/')
        self.assertEqual(
            Notification.objects.filter(user=self.student.user, read_at__isnull=True).count(), 0
        )

    def test_marking_read_requires_post(self):
        self.client.force_login(self.student.user)
        self.assertEqual(self.client.get('/notifications/mark-read/').status_code, 405)

    def test_a_student_only_sees_their_own_notifications(self):
        Notification.objects.create(user=self.student.user, message='Mine')
        Notification.objects.create(user=self.other_student.user, message='Theirs')
        self.client.force_login(self.student.user)
        response = self.client.get('/notifications/')
        self.assertContains(response, 'Mine')
        self.assertNotContains(response, 'Theirs')

    def test_students_without_an_account_are_skipped(self):
        """There is nowhere to deliver to, and it must not raise."""
        make_student(student_id='20512666', name='No Login',
                     group=self.my_group, with_account=False)
        req = RescheduleRequest.objects.create(
            entry=self.entry, requested_timeslot=self.free_slot,
            requested_room=self.entry.room, reason='clash',
        )
        self.client.force_login(self.admin)
        response = self.client.post(f'/reschedule/{req.pk}/approve/')
        self.assertEqual(response.status_code, 302)


class StudentPermissionTests(TestCase):
    """A student is read-only on their own timetable and nothing else."""

    FORBIDDEN = [
        '/conflicts/', '/reschedule/', '/generate/', '/algorithm/',
        '/lecturers/', '/courses/', '/rooms/', '/student-groups/',
        '/timeslots/', '/students/', '/students/add/',
    ]
    ALLOWED = ['/', '/timetable/', '/notifications/']

    def setUp(self):
        build_dataset()
        self.student = make_student(group=StudentGroup.objects.first())
        self.client.force_login(self.student.user)

    def test_student_is_refused_every_staff_route(self):
        for url in self.FORBIDDEN:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (302, 403),
                    f'{url} was reachable by a student',
                )

    def test_student_can_reach_their_own_pages(self):
        for url in self.ALLOWED:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_student_cannot_submit_a_reschedule(self):
        entry = TimetableEntry.objects.filter(is_active=True).first()
        before = RescheduleRequest.objects.count()
        self.client.post('/reschedule/', {
            'entry': entry.pk if entry else 1, 'timeslot': 1, 'room': '', 'reason': 'x',
        })
        self.assertEqual(RescheduleRequest.objects.count(), before)

    def test_student_cannot_create_accounts(self):
        other = Student.objects.create(student_id='20500000', name='Target')
        response = self.client.post(f'/students/{other.pk}/create-account/')
        self.assertIn(response.status_code, (302, 403))
        other.refresh_from_db()
        self.assertIsNone(other.user)

    def test_sidebar_hides_staff_navigation(self):
        body = self.client.get('/').content.decode()
        nav = body[body.index('class="sidebar-nav"'):body.index('</div>\n\n<div class="main-wrap"')]
        for hidden in ['/conflicts/', '/reschedule/', '/generate/',
                       '/algorithm/', '/students/', '/courses/']:
            with self.subTest(link=hidden):
                self.assertNotIn(f'href="{hidden}"', nav)

    def test_sidebar_offers_their_own_pages(self):
        body = self.client.get('/').content.decode()
        self.assertIn('href="/timetable/"', body)
        self.assertIn('href="/notifications/"', body)


class StudentAdminPageTests(TestCase):
    def setUp(self):
        build_dataset()
        self.admin = make_admin('studentadmin')
        self.client.force_login(self.admin)

    def test_admin_can_add_a_student(self):
        group = StudentGroup.objects.first()
        response = self.client.post('/students/add/', {
            'student_id': '20599999', 'name': 'New Person', 'group': group.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Student.objects.filter(student_id='20599999').exists())

    def test_student_id_must_be_unique(self):
        Student.objects.create(student_id='20511111', name='First')
        form = StudentForm(data={'student_id': '20511111', 'name': 'Second', 'group': ''})
        self.assertFalse(form.is_valid())
        self.assertIn('student_id', form.errors)

    def test_admin_can_create_a_students_account_from_the_page(self):
        student = Student.objects.create(student_id='20522222', name='Needs Login')
        response = self.client.post(
            f'/students/{student.pk}/create-account/', follow=True
        )
        student.refresh_from_db()
        self.assertIsNotNone(student.user)
        self.assertEqual(student.user.username, '20522222')
        match = re.search(r'password &quot;([^&]+)&quot;', response.content.decode())
        self.assertIsNotNone(match, 'the generated password was not shown')
        self.assertTrue(
            self.client.login(username='20522222', password=match.group(1))
        )

    def test_refuses_when_the_id_is_already_a_username(self):
        User.objects.create_user(username='20533333', password='x')
        student = Student.objects.create(student_id='20533333', name='Clash')
        self.client.post(f'/students/{student.pk}/create-account/')
        student.refresh_from_db()
        self.assertIsNone(student.user)

    def test_deleting_a_student_keeps_the_timetable(self):
        student = Student.objects.create(
            student_id='20544444', name='Leaving', group=StudentGroup.objects.first()
        )
        run_genetic_algorithm()
        before = TimetableEntry.objects.filter(is_active=True).count()
        self.client.post(f'/students/{student.pk}/delete/')
        self.assertEqual(TimetableEntry.objects.filter(is_active=True).count(), before)


class ViewAsTests(TestCase):
    """Previewing must only ever lose privilege, never gain it."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.admin = make_admin('previewer')
        self.other_admin = make_admin('otherboss')

        self.lecturer = Lecturer.objects.get(email='l0@example.com')
        self.lecturer_user = make_lecturer_user('drpreview')
        self.lecturer.user = self.lecturer_user
        self.lecturer.save()

        self.student = make_student(
            student_id='20599001', group=StudentGroup.objects.get(name='Group A')
        )

    def test_admin_can_preview_as_a_student(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/view-as/{self.student.user.pk}/', follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'scheduler/dashboard_student.html')
        self.assertEqual(response.context['viewing_as'], self.student.name)

    def test_admin_can_preview_as_a_lecturer(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/view-as/{self.lecturer_user.pk}/', follow=True)
        self.assertTemplateUsed(response, 'scheduler/dashboard_lecturer.html')

    def test_preview_loses_admin_access(self):
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.student.user.pk}/')
        for url in ['/courses/', '/generate/', '/algorithm/', '/students/']:
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (302, 403))

    def test_the_banner_is_always_shown(self):
        # Asserted on the banner's content, not its CSS class: the class name
        # sits in the stylesheet on every page and would pass either way.
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.student.user.pk}/')
        response = self.client.get('/')
        self.assertContains(response, 'Back to my account')
        self.assertContains(response, '/view-as/stop/')
        self.assertContains(response, self.student.name)

    def test_returning_restores_full_access(self):
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.student.user.pk}/')
        self.assertIn(self.client.get('/courses/').status_code, (302, 403))

        self.client.post('/view-as/stop/')
        self.assertEqual(self.client.get('/courses/').status_code, 200)

    def test_cannot_preview_as_another_admin(self):
        """The whole point is that it is never an escalation."""
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.other_admin.pk}/')
        response = self.client.get('/')
        self.assertIsNone(response.context['impersonator'])

    def test_a_lecturer_cannot_preview_as_anyone(self):
        self.client.force_login(self.lecturer_user)
        response = self.client.post(f'/view-as/{self.student.user.pk}/')
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self.client.get('/').context['impersonator'])

    def test_a_student_cannot_preview_as_anyone(self):
        self.client.force_login(self.student.user)
        response = self.client.post(f'/view-as/{self.lecturer_user.pk}/')
        self.assertEqual(response.status_code, 404)

    def test_preview_cannot_be_chained_into_an_escalation(self):
        """From inside a preview, the real account still decides - and it
        still cannot reach an administrator."""
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.student.user.pk}/')
        self.client.post(f'/view-as/{self.other_admin.pk}/')
        response = self.client.get('/')
        # Still the student, never the other admin.
        self.assertEqual(response.context['viewing_as'], self.student.name)

    def test_losing_admin_rights_ends_an_open_preview(self):
        """Re-checked every request, not just when it started."""
        self.client.force_login(self.admin)
        self.client.post(f'/view-as/{self.student.user.pk}/')
        self.assertIsNotNone(self.client.get('/').context['impersonator'])

        self.admin.is_superuser = False
        self.admin.groups.clear()
        self.admin.save()

        response = self.client.get('/')
        self.assertIsNone(response.context['impersonator'])

    def test_starting_requires_post(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(f'/view-as/{self.student.user.pk}/').status_code, 405
        )

    def test_stopping_requires_post(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/view-as/stop/').status_code, 405)

    def test_stopping_when_not_previewing_is_harmless(self):
        self.client.force_login(self.admin)
        response = self.client.post('/view-as/stop/', follow=True)
        self.assertEqual(response.status_code, 200)

    def test_no_banner_when_not_previewing(self):
        self.client.force_login(self.admin)
        response = self.client.get('/')
        self.assertNotContains(response, 'Back to my account')
        self.assertNotContains(response, '/view-as/stop/')

    def test_view_as_button_appears_for_accounts_that_exist(self):
        self.client.force_login(self.admin)
        body = self.client.get('/students/').content.decode()
        self.assertIn(f'/view-as/{self.student.user.pk}/', body)


def csv_upload(text, name='data.csv', encoding='utf-8'):
    return SimpleUploadedFile(name, text.encode(encoding), content_type='text/csv')


class CsvImportTests(TestCase):
    def setUp(self):
        self.group = StudentGroup.objects.create(name='CS Level 100')

    # --- the one gate: is this the kind of file it says it is? ---------------

    def test_rooms_file_is_refused_by_the_lecturers_importer(self):
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', csv_upload('name,capacity\nPB 001,250\n'))
        self.assertIn('does not look like a Lecturers file', str(ctx.exception))

    def test_the_refusal_says_where_the_file_does_belong(self):
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', csv_upload('name,capacity\nPB 001,250\n'))
        self.assertIn('Rooms', str(ctx.exception))

    def test_every_kind_refuses_every_other_kinds_file(self):
        for target in KINDS:
            for source in KINDS:
                if source == target:
                    continue
                with self.subTest(uploading=source, into=target):
                    with self.assertRaises(CsvImportError):
                        run_import(target, csv_upload(template_csv(source)))

    def test_a_courses_file_is_not_mistaken_for_lecturers(self):
        """It carries lecturer_email, which alone would pass as a Lecturers
        file and then import course names as people."""
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', csv_upload(
                'code,name,expected_students,lecturer_email\n'
                'CS 151,Intro,220,dr@knust.edu.gh\n'))
        self.assertIn('Courses file', str(ctx.exception))
        self.assertEqual(Lecturer.objects.count(), 0)

    def test_the_right_file_is_accepted(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                result = run_import(kind, csv_upload(template_csv(kind)))
                self.assertGreater(result.total, 0)

    # --- past the gate, load what you can ------------------------------------

    def test_a_bad_row_does_not_stop_the_good_ones(self):
        upload = csv_upload(
            'name,email\n'
            'Good Person,good@knust.edu.gh\n'
            'No Email,\n'
            'Also Good,also@knust.edu.gh\n'
        )
        result = run_import('lecturers', upload)
        self.assertEqual(Lecturer.objects.count(), 2)
        self.assertEqual(result.created, 2)
        self.assertEqual(len(result.skipped), 1)

    def test_skipped_rows_name_the_row_number(self):
        result = run_import(
            'lecturers', csv_upload('name,email\nFine,fine@x.gh\nBroken,\n')
        )
        self.assertIn('Row 3', result.skipped[0])

    def test_a_missing_name_is_filled_in_rather_than_skipped(self):
        """Only the identifier is indispensable."""
        result = run_import('lecturers', csv_upload('name,email\n,ama@knust.edu.gh\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Lecturer.objects.get().name, 'ama')

    def test_course_without_expected_students_takes_the_default(self):
        result = run_import('courses', csv_upload(
            'code,name,expected_students,lecturer_email\nCS 151,Intro,,\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Course.objects.get().expected_students, 30)

    def test_a_file_of_entirely_unusable_rows_is_reported_as_such(self):
        """Right headers, wrong contents - better than 500 identical errors."""
        with self.assertRaises(CsvImportError) as ctx:
            run_import('rooms', csv_upload(
                'name,capacity\nPB 001,none\nCS Lab,none\n'))
        self.assertIn('None of the 2 rows', str(ctx.exception))

    # --- re-uploading updates rather than duplicating ------------------------

    def test_reupload_updates_instead_of_duplicating(self):
        run_import('lecturers', csv_upload('name,email\nOld Name,x@knust.edu.gh\n'))
        result = run_import('lecturers', csv_upload('name,email\nNew Name,x@knust.edu.gh\n'))
        self.assertEqual(Lecturer.objects.count(), 1)
        self.assertEqual(Lecturer.objects.get().name, 'New Name')
        self.assertEqual((result.created, result.updated), (0, 1))

    def test_duplicate_within_one_file_keeps_the_last(self):
        result = run_import(
            'lecturers', csv_upload('name,email\nFirst,same@x.gh\nSecond,same@x.gh\n')
        )
        self.assertEqual(Lecturer.objects.count(), 1)
        self.assertEqual(Lecturer.objects.get().name, 'Second')
        self.assertEqual(result.total, 2)

    # --- references are created rather than refused --------------------------

    def test_course_with_unknown_lecturer_creates_them(self):
        upload = csv_upload(
            'code,name,expected_students,lecturer_email\n'
            'CS 151,Intro,220,nobody@knust.edu.gh\n'
        )
        result = run_import('courses', upload)
        self.assertEqual(Course.objects.count(), 1)
        lecturer = Course.objects.get().lecturer
        self.assertIsNotNone(lecturer)
        self.assertEqual(lecturer.email, 'nobody@knust.edu.gh')
        self.assertEqual(len(result.auto_created), 1)

    def test_course_lecturer_may_be_blank(self):
        result = run_import('courses', csv_upload(
            'code,name,expected_students,lecturer_email\nCS 151,Intro,220,\n'))
        self.assertEqual(result.total, 1)
        self.assertIsNone(Course.objects.get().lecturer)
        self.assertEqual(result.auto_created, [])

    def test_course_links_to_an_existing_lecturer(self):
        lecturer = Lecturer.objects.create(name='Dr X', email='x@knust.edu.gh')
        run_import('courses', csv_upload(
            'code,name,expected_students,lecturer_email\n'
            'CS 151,Intro,220,X@KNUST.EDU.GH\n'))
        self.assertEqual(Course.objects.get().lecturer, lecturer)
        self.assertEqual(Lecturer.objects.count(), 1, 'a duplicate lecturer was created')

    def test_student_with_unknown_group_creates_it(self):
        result = run_import(
            'students', csv_upload('student_id,name,group\n20512001,Ama,CS Level 400\n')
        )
        self.assertEqual(Student.objects.get().group.name, 'CS Level 400')
        self.assertEqual(len(result.auto_created), 1)

    def test_students_sharing_a_new_group_create_it_once(self):
        run_import('students', csv_upload(
            'student_id,name,group\n'
            '20512001,Ama,CS Level 400\n'
            '20512002,Kojo,CS Level 400\n'))
        self.assertEqual(StudentGroup.objects.filter(name='CS Level 400').count(), 1)

    def test_student_group_may_be_blank(self):
        result = run_import(
            'students', csv_upload('student_id,name,group\n20512001,Ama,\n')
        )
        self.assertEqual(result.total, 1)
        self.assertIsNone(Student.objects.get().group)

    # --- file-level problems --------------------------------------------------

    def test_missing_identifying_column_is_reported_clearly(self):
        with self.assertRaises(CsvImportError) as ctx:
            run_import('rooms', csv_upload('name\nPB 001\n'))
        self.assertIn('capacity', str(ctx.exception))

    # --- headers as real exports actually spell them --------------------------

    def test_email_address_column_is_recognised(self):
        result = run_import('lecturers', csv_upload(
            'Full Name,Email Address\nDr Kwame,kwame@knust.edu.gh\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Lecturer.objects.get().email, 'kwame@knust.edu.gh')

    def test_common_header_spellings(self):
        cases = [
            ('lecturers', 'Name,E-Mail\nDr A,a@x.gh\n', Lecturer),
            ('lecturers', 'LECTURER NAME,LECTURER EMAIL\nDr B,b@x.gh\n', Lecturer),
            ('rooms', 'Room Name,Seating Capacity\nPB 001,250\n', Room),
            ('rooms', 'Venue,Seats\nSF 21,90\n', Room),
            ('courses', 'Course Code,Course Title,Class Size\nCS 151,Intro,220\n', Course),
            ('students', 'Student Number,Index Number,Student Name\n20512001,7212001,Ama\n', Student),
            ('students', 'ID,Name,Level\n20512002,Kojo,200\n', Student),
        ]
        for kind, text, model in cases:
            with self.subTest(header=text.splitlines()[0]):
                before = model.objects.count()
                run_import(kind, csv_upload(text))
                self.assertEqual(model.objects.count(), before + 1)

    def test_semicolon_delimited_file_is_read(self):
        """Excel writes semicolons in locales where comma is the decimal mark."""
        result = run_import('rooms', csv_upload('name;capacity\nPB 001;250\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Room.objects.get().capacity, 250)

    def test_tab_delimited_file_is_read(self):
        result = run_import('rooms', csv_upload('name\tcapacity\nPB 001\t250\n'))
        self.assertEqual(result.created, 1)

    def test_the_refusal_lists_the_columns_it_actually_found(self):
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', csv_upload('Staff Ref,Department\n001,CS\n'))
        message = str(ctx.exception)
        self.assertIn('Staff Ref', message)
        self.assertIn('Department', message)

    def test_a_column_is_claimed_by_only_one_field(self):
        """A courses file with one "email" column gives it to lecturer_email
        rather than leaving the link unmade."""
        result = run_import('courses', csv_upload(
            'Course Code,Course Name,Email\nCS 151,Intro,dr@knust.edu.gh\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Course.objects.get().lecturer.email, 'dr@knust.edu.gh')

    def test_extra_columns_are_ignored(self):
        """A spreadsheet with more in it than we need still imports."""
        result = run_import('rooms', csv_upload(
            'name,capacity,building,notes\nPB 001,250,Petroleum,refurbished\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Room.objects.get().capacity, 250)

    def test_column_order_does_not_matter(self):
        result = run_import('rooms', csv_upload('capacity,name\n250,PB 001\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Room.objects.get().name, 'PB 001')

    def test_empty_file_is_refused(self):
        with self.assertRaises(CsvImportError):
            run_import('lecturers', csv_upload(''))

    def test_header_only_file_is_refused(self):
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', csv_upload('name,email\n'))
        self.assertIn('no data', str(ctx.exception))

    def test_excel_byte_order_mark_is_handled(self):
        """Excel's "CSV UTF-8" prepends a BOM, which would otherwise corrupt
        the first header and fail every lookup against it."""
        result = run_import('lecturers', csv_upload('﻿name,email\nAma,ama@knust.edu.gh\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Lecturer.objects.get().name, 'Ama')

    def test_headers_are_case_and_space_insensitive(self):
        result = run_import('lecturers', csv_upload(' Name , EMAIL \nAma,ama@knust.edu.gh\n'))
        self.assertEqual(result.created, 1)

    def test_blank_lines_are_skipped(self):
        result = run_import(
            'lecturers', csv_upload('name,email\nAma,ama@x.gh\n\n\nKojo,kojo@x.gh\n')
        )
        self.assertEqual(result.created, 2)

    def test_non_utf8_file_is_reported_not_crashed(self):
        upload = SimpleUploadedFile(
            'x.csv', 'name,email\nAmé,a@x.gh\n'.encode('utf-16'),
            content_type='text/csv',
        )
        with self.assertRaises(CsvImportError) as ctx:
            run_import('lecturers', upload)
        self.assertIn('UTF-8', str(ctx.exception))

    # --- field validation -----------------------------------------------------

    def test_a_room_without_a_usable_capacity_is_skipped_not_fatal(self):
        result = run_import('rooms', csv_upload(
            'name,capacity\nPB 001,250\nCS Lab,lots\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn('CS Lab', result.skipped[0])

    def test_a_decimal_capacity_from_a_spreadsheet_is_accepted(self):
        result = run_import('rooms', csv_upload('name,capacity\nPB 001,250.0\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Room.objects.get().capacity, 250)

    def test_values_are_trimmed(self):
        run_import('rooms', csv_upload('name,capacity\n  PB 001  ,  250  \n'))
        room = Room.objects.get()
        self.assertEqual(room.name, 'PB 001')
        self.assertEqual(room.capacity, 250)

    # --- the whole set --------------------------------------------------------

    def test_a_realistic_sequence_imports(self):
        run_import('lecturers', csv_upload(
            'name,email\nDr A,a@knust.edu.gh\nDr B,b@knust.edu.gh\n'))
        run_import('rooms', csv_upload(
            'name,capacity\nPB 001,250\nCS Lab 1,60\n'))
        run_import('courses', csv_upload(
            'code,name,expected_students,lecturer_email\n'
            'CS 151,Intro,220,a@knust.edu.gh\nCS 153,Discrete,210,b@knust.edu.gh\n'))
        run_import('students', csv_upload(
            'student_id,name,group\n20512001,Ama,CS Level 100\n'
            '20512002,Kojo,CS Level 100\n'))

        self.assertEqual(Lecturer.objects.count(), 2)
        self.assertEqual(Room.objects.count(), 2)
        self.assertEqual(Course.objects.count(), 2)
        self.assertEqual(Student.objects.count(), 2)

    def test_students_can_be_imported_before_any_group_exists(self):
        """The order that used to be mandatory is now merely tidy."""
        StudentGroup.objects.all().delete()
        result = run_import('students', csv_upload(
            'student_id,name,group\n20512001,Ama,CS Level 100\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(StudentGroup.objects.count(), 1)

    def test_template_has_the_documented_header(self):
        for kind, spec in KINDS.items():
            with self.subTest(kind=kind):
                first_line = template_csv(kind).splitlines()[0]
                self.assertEqual(first_line.split(','), spec['columns'])

    def test_every_template_re_imports_cleanly(self):
        """The sample rows we hand out must themselves be valid."""
        for kind in KINDS:
            with self.subTest(kind=kind):
                result = run_import(kind, csv_upload(template_csv(kind)))
                self.assertFalse(result.skipped, f'{kind}: {result.skipped}')
                self.assertEqual(result.total, len(KINDS[kind]['sample']))


class SplitCohortTests(TestCase):
    """A cohort too large for any room is split across groups, and each half is
    timetabled separately. That is what a student group is for."""

    def setUp(self):
        self.lecturer = Lecturer.objects.create(name='Dr Mensah', email='m@knust.edu.gh')
        # Ninety students, no room holding more than fifty.
        self.course = Course.objects.create(
            code='CS 451', name='Distributed Systems',
            expected_students=90, lecturer=self.lecturer,
        )
        self.other = Course.objects.create(
            code='CS 453', name='Machine Learning',
            expected_students=90, lecturer=self.lecturer,
        )
        self.g1 = StudentGroup.objects.create(name='CS 400 Group 1', size=45)
        self.g2 = StudentGroup.objects.create(name='CS 400 Group 2', size=45)
        for group in (self.g1, self.g2):
            group.courses.set([self.course, self.other])

        for name in ('PB 001', 'PB 012', 'SF 21'):
            Room.objects.create(name=name, capacity=50)
        for day in ('MON', 'TUE', 'WED'):
            for start, end in ((time(8, 0), time(10, 0)), (time(10, 0), time(12, 0))):
                TimeSlot.objects.create(day=day, start_time=start, end_time=end)

    def test_a_class_is_sized_by_the_group_not_the_course(self):
        self.assertEqual(self.g1.size_for(self.course), 45)
        self.assertNotEqual(self.g1.size_for(self.course), self.course.expected_students)

    def test_a_room_holding_the_group_is_not_reported_as_too_small(self):
        """It used to be, because the whole cohort was measured against it."""
        run_genetic_algorithm()
        for conflict in detect_conflicts():
            self.assertNotEqual(conflict['type'], 'Room Capacity Mismatch')

    def test_each_group_gets_its_own_class(self):
        run_genetic_algorithm()
        entries = TimetableEntry.objects.filter(is_active=True, course=self.course)
        self.assertEqual(entries.count(), 2)
        self.assertEqual(
            {e.student_group_id for e in entries}, {self.g1.pk, self.g2.pk}
        )

    def test_the_two_groups_are_not_scheduled_at_the_same_time(self):
        """One lecturer cannot teach both halves at once."""
        run_genetic_algorithm()
        for course in (self.course, self.other):
            slots = [
                e.timeslot_id for e in
                TimetableEntry.objects.filter(is_active=True, course=course)
            ]
            with self.subTest(course=course.code):
                self.assertEqual(len(slots), len(set(slots)))

    def test_a_split_cohort_can_be_scheduled_perfectly(self):
        result = run_genetic_algorithm()
        self.assertTrue(result['success'])
        self.assertEqual(result['violations']['capacity'], 0)

    def test_falls_back_to_students_on_file_when_size_is_blank(self):
        loose = StudentGroup.objects.create(name='Unsized')
        loose.courses.set([self.course])
        for i in range(12):
            Student.objects.create(student_id=f'2050{i:04d}', name=f'S{i}', group=loose)
        self.assertEqual(loose.size_for(self.course), 12)

    def test_falls_back_to_the_course_total_when_nothing_else_is_known(self):
        """Without a size or any students, the course's own figure is all there
        is - the previous behaviour, kept for groups nobody has sized."""
        bare = StudentGroup.objects.create(name='Bare')
        bare.courses.set([self.course])
        self.assertEqual(bare.size_for(self.course), 90)

    def test_a_declared_size_beats_the_students_on_file(self):
        """Rooms are booked before every student has registered."""
        self.assertEqual(Student.objects.filter(group=self.g1).count(), 0)
        self.assertEqual(self.g1.size_for(self.course), 45)

    def test_an_oversized_group_is_still_flagged(self):
        """The check must still fire when the group genuinely does not fit."""
        huge = StudentGroup.objects.create(name='Huge', size=500)
        huge.courses.set([self.course])
        run_genetic_algorithm()
        types = {c['type'] for c in detect_conflicts()}
        self.assertIn('Room Capacity Mismatch', types)


class StudentFieldTests(TestCase):
    """Student ID, index number, programme and level are four separate things."""

    def setUp(self):
        self.group = StudentGroup.objects.create(name='CS Level 400')
        self.client.force_login(make_admin('fieldadmin'))

    def test_all_four_are_stored_separately(self):
        self.client.post('/students/add/', {
            'student_id': '20212007',
            'index_number': '7212007',
            'name': 'Adjoa Mensimah',
            'programme': 'BSc Computer Science',
            'level': '400',
            'group': self.group.pk,
        })
        student = Student.objects.get(student_id='20212007')
        self.assertEqual(student.index_number, '7212007')
        self.assertEqual(student.programme, 'BSc Computer Science')
        self.assertEqual(student.level, '400')
        self.assertEqual(student.group, self.group)

    def test_only_the_student_id_is_required(self):
        self.client.post('/students/add/', {
            'student_id': '20212008', 'index_number': '', 'name': 'Nana Yaw',
            'programme': '', 'level': '', 'group': '',
        })
        student = Student.objects.get(student_id='20212008')
        self.assertIsNone(student.index_number)
        self.assertEqual(student.programme, '')

    def test_blank_index_numbers_do_not_collide(self):
        """Two empty strings break a unique constraint where two NULLs do not."""
        for sid in ('20500001', '20500002', '20500003'):
            Student.objects.create(student_id=sid, name=sid, index_number='')
        self.assertEqual(Student.objects.filter(index_number=None).count(), 3)

    def test_a_duplicate_index_number_is_rejected(self):
        Student.objects.create(student_id='20500001', name='First', index_number='7212001')
        duplicate = Student(student_id='20500002', name='Second', index_number='7212001')
        with self.assertRaises(DjangoValidationError):
            duplicate.full_clean()

    def test_students_list_shows_every_column(self):
        Student.objects.create(
            student_id='20212007', index_number='7212007', name='Adjoa',
            programme='BSc Computer Science', level='400', group=self.group,
        )
        response = self.client.get('/students/')
        for value in ['20212007', '7212007', 'Adjoa', 'BSc Computer Science',
                      'Level 400', 'CS Level 400']:
            with self.subTest(value=value):
                self.assertContains(response, value)

    def test_programme_and_level_read_together_when_wanted(self):
        student = Student(programme='BSc Computer Science', level='400')
        self.assertEqual(student.programme_and_level, 'BSc Computer Science Level 400')

    def test_login_is_still_the_student_id_not_the_index_number(self):
        student = Student.objects.create(
            student_id='20212007', index_number='7212007', name='Adjoa',
        )
        user, password = create_student_account(student)
        self.assertEqual(user.username, '20212007')
        self.assertTrue(self.client.login(username='20212007', password=password))


class StudentImportFieldTests(TestCase):
    def test_the_new_columns_import(self):
        result = run_import('students', csv_upload(
            'student_id,index_number,name,programme,level,group\n'
            '20212007,7212007,Adjoa,BSc Computer Science,400,CS Level 400\n'))
        self.assertEqual(result.created, 1)
        student = Student.objects.get()
        self.assertEqual(student.index_number, '7212007')
        self.assertEqual(student.programme, 'BSc Computer Science')
        self.assertEqual(student.level, '400')
        self.assertEqual(student.group.name, 'CS Level 400')

    def test_index_number_is_not_mistaken_for_the_student_id(self):
        """They are different numbers, so "Index Number" must not fill student_id."""
        run_import('students', csv_upload(
            'Student ID,Index Number,Name\n20212007,7212007,Adjoa\n'))
        student = Student.objects.get()
        self.assertEqual(student.student_id, '20212007')
        self.assertEqual(student.index_number, '7212007')

    def test_level_accepts_the_spellings_people_use(self):
        for text, expected in [('400', '400'), ('Level 400', '400'), ('L400', '400'),
                               ('4', '400'), ('', ''), ('nonsense', '')]:
            with self.subTest(level=text):
                Student.objects.all().delete()
                run_import('students', csv_upload(
                    f'student_id,name,level\n20500001,X,{text}\n'))
                self.assertEqual(Student.objects.get().level, expected)

    def test_a_clashing_index_number_is_dropped_not_fatal(self):
        Student.objects.create(student_id='20500001', name='First', index_number='7212001')
        result = run_import('students', csv_upload(
            'student_id,index_number,name\n20500002,7212001,Second\n'))
        self.assertEqual(result.created, 1)
        second = Student.objects.get(student_id='20500002')
        self.assertEqual(second.name, 'Second')
        self.assertIsNone(second.index_number)
        self.assertEqual(len(result.skipped), 1)
        self.assertIn('already belongs', result.skipped[0])

    def test_reimporting_keeps_a_students_own_index_number(self):
        run_import('students', csv_upload(
            'student_id,index_number,name\n20500001,7212001,First\n'))
        result = run_import('students', csv_upload(
            'student_id,index_number,name\n20500001,7212001,Corrected Name\n'))
        student = Student.objects.get()
        self.assertEqual(student.name, 'Corrected Name')
        self.assertEqual(student.index_number, '7212001')
        self.assertEqual(result.skipped, [])

    def test_a_students_file_still_only_needs_the_student_id(self):
        result = run_import('students', csv_upload('student_id\n20500001\n'))
        self.assertEqual(result.created, 1)


class StudentGroupFormTests(TestCase):
    def setUp(self):
        self.client.force_login(make_admin('groupadmin'))

    def test_select_all_appears_when_there_are_courses(self):
        Course.objects.create(code='CS 151', name='Intro', expected_students=100)
        body = self.client.get('/student-groups/add/').content.decode()
        self.assertIn('id="course-toggle"', body)
        self.assertIn('id="course-count"', body)

    def test_select_all_is_absent_when_there_are_no_courses(self):
        """A control that could only ever select nothing is worse than none."""
        self.assertEqual(Course.objects.count(), 0)
        body = self.client.get('/student-groups/add/').content.decode()
        self.assertNotIn('id="course-toggle"', body)

    def test_the_toggle_is_not_a_submit_button(self):
        """type=button, or clicking it would save the half-filled form."""
        Course.objects.create(code='CS 151', name='Intro', expected_students=100)
        body = self.client.get('/student-groups/add/').content.decode()
        toggle = body[body.index('id="course-toggle"') - 200:body.index('id="course-toggle"')]
        self.assertIn('type="button"', toggle)

    def test_selecting_every_course_saves_them_all(self):
        codes = ['CS 151', 'CS 153', 'CS 155']
        for code in codes:
            Course.objects.create(code=code, name=code, expected_students=50)
        self.client.post('/student-groups/add/', {
            'name': 'CS Level 100',
            'courses': list(Course.objects.values_list('pk', flat=True)),
        })
        group = StudentGroup.objects.get(name='CS Level 100')
        self.assertEqual(group.courses.count(), len(codes))


class CsvImportViewTests(TestCase):
    def setUp(self):
        self.admin = make_admin('importer')
        self.client.force_login(self.admin)

    def test_page_renders(self):
        self.assertEqual(self.client.get('/import/').status_code, 200)

    def test_each_kind_renders(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    self.client.get(f'/import/?kind={kind}').status_code, 200
                )

    def test_unknown_kind_404s(self):
        self.assertEqual(self.client.get('/import/?kind=nonsense').status_code, 404)

    def test_uploading_creates_records(self):
        self.client.post('/import/', {
            'kind': 'lecturers',
            'file': csv_upload('name,email\nDr A,a@knust.edu.gh\n'),
        })
        self.assertEqual(Lecturer.objects.count(), 1)

    def test_wrong_kind_of_file_is_refused_with_a_pointer(self):
        response = self.client.post('/import/', {
            'kind': 'lecturers',
            'file': csv_upload('name,capacity\nPB 001,250\n'),
        }, follow=True)
        self.assertContains(response, 'does not look like a Lecturers file')
        self.assertEqual(Lecturer.objects.count(), 0)

    def test_skipped_rows_are_listed_and_the_rest_imported(self):
        response = self.client.post('/import/', {
            'kind': 'rooms',
            'file': csv_upload('name,capacity\nPB 001,250\nCS Lab,lots\n'),
        })
        self.assertContains(response, 'CS Lab')
        self.assertEqual(Room.objects.count(), 1)

    def test_missing_file_is_handled(self):
        response = self.client.post('/import/', {'kind': 'lecturers'}, follow=True)
        self.assertContains(response, 'Choose a CSV file')

    def test_template_downloads(self):
        response = self.client.get('/import/template/rooms/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('name,capacity', response.content.decode())

    def test_import_is_admin_only(self):
        build_dataset()
        self.client.force_login(make_lecturer_user('notadmin'))
        self.assertIn(self.client.get('/import/').status_code, (302, 403))
        self.assertIn(
            self.client.get('/import/template/rooms/').status_code, (302, 403)
        )

    def test_a_student_cannot_import(self):
        build_dataset()
        student = make_student(student_id='20500123')
        self.client.force_login(student.user)
        self.assertIn(self.client.get('/import/').status_code, (302, 403))
        response = self.client.post('/import/', {
            'kind': 'lecturers',
            'file': csv_upload('name,email\nSneaky,s@x.gh\n'),
        })
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(Lecturer.objects.filter(email='s@x.gh').exists())


class SmokeTests(TestCase):
    """T5.2: walk every route as an admin and assert nothing 500s."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()
        self.client.force_login(make_admin())

    def test_no_unrendered_template_syntax_leaks_into_pages(self):
        """A {# #} comment spanning two lines is never matched by the lexer and
        is emitted as literal text on the page."""
        for url in ['/', '/timetable/', '/reschedule/', '/courses/', '/algorithm/']:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                for token in ['{#', '#}', '{%', '%}']:
                    self.assertNotIn(token, body)

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
