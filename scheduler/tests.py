import random
from datetime import time

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import TestCase

from . import charts
from .baselines import compare, greedy_schedule, random_schedule
from .charts import convergence_chart
from .conflict_detector import detect_conflicts
from .forms import TimeSlotForm
from .genetic_algorithm import fitness, load_problem, run_genetic_algorithm
from .models import (
    Course, GenerationRun, Lecturer, RescheduleRequest, Room, StudentGroup,
    TimeSlot, TimetableEntry,
)
from .permissions import ADMIN_GROUP, is_admin, lecturer_for

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

    def test_account_with_no_lecturer_gets_nothing(self):
        """Unlinked accounts must see no classes, not every class."""
        self.client.force_login(make_lecturer_user('unlinked'))
        response = self.client.get('/reschedule/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['entries']), 0)
        self.assertEqual(self._submit(self.my_entry).status_code, 404)
        self.assertEqual(RescheduleRequest.objects.count(), 0)

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

    def test_lecturer_sees_only_their_own_pending_requests(self):
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
        pending = self.client.get('/reschedule/').context['pending_requests']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].requested_by, self.user)

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
    OPEN_TO_ANY_USER = ['/', '/timetable/', '/conflicts/', '/reschedule/']

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

    def test_shared_routes_are_open_to_any_user(self):
        self.client.force_login(self.lecturer_user)
        for url in self.OPEN_TO_ANY_USER:
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
