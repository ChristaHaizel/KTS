import csv
import importlib
import io
import json
import os
import random
import re
import smtplib
import urllib.error
from datetime import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.contrib.staticfiles import finders
from django.core import mail
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.test.utils import CaptureQueriesContext

from . import charts
from .accounts import create_student_account
from .baselines import compare, greedy_schedule, random_schedule
from .mail import (
    BrevoBackend, MailDeliveryError, ResendBackend, brevo_api_key,
    resend_api_key,
)
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
    College, Course, Department, GenerationRun, Lecturer, Notification,
    RescheduleRequest, Room, Student, StudentGroup, TimeSlot, TimetableEntry,
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

    def test_grid_has_one_row_per_day_and_one_column_per_period(self):
        """Days down the side, times across the top. 5 days x 4 periods is 5
        rows of 4, not one row per TimeSlot."""
        self.assertEqual(TimeSlot.objects.count(), len(DAYS) * len(PERIODS))
        response = self.client.get('/timetable/')
        self.assertEqual(response.status_code, 200)
        grid = response.context['grid']
        self.assertEqual(len(grid), len(DAYS))
        for row in grid:
            self.assertEqual(len(row['cells']), len(PERIODS))
        self.assertEqual(len(response.context['periods']), len(PERIODS))

    def test_the_rows_are_the_weekdays_in_order(self):
        rows = self.client.get('/timetable/').context['grid']
        self.assertEqual([r['day'] for r in rows], DAYS)
        self.assertEqual(rows[0]['day_name'], 'Monday')

    def test_the_columns_are_the_periods_in_time_order(self):
        periods = self.client.get('/timetable/').context['periods']
        starts = [p['start'] for p in periods]
        self.assertEqual(starts, sorted(starts))

    def test_an_entry_lands_in_the_cell_for_its_day_and_period(self):
        run_genetic_algorithm()
        response = self.client.get('/timetable/')
        periods = response.context['periods']
        for row in response.context['grid']:
            for index, cell in enumerate(row['cells']):
                for entry in cell:
                    self.assertEqual(entry.timeslot.day, row['day'])
                    self.assertEqual(entry.timeslot.start_time, periods[index]['start'])

    def test_every_active_entry_appears_exactly_once(self):
        run_genetic_algorithm()
        grid = self.client.get('/timetable/').context['grid']
        shown = [e.pk for row in grid for cell in row['cells'] for e in cell]
        self.assertEqual(sorted(shown), sorted(
            TimetableEntry.objects.filter(is_active=True).values_list('pk', flat=True)
        ))

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
        department = Department.objects.get(name='Computer Science')
        response = self.client.post('/students/add/', {
            'student_id': '20599999',
            'index_number': '7599999',
            'name': 'New Person',
            'email': 'new@st.knust.edu.gh',
            'college': department.college.pk,
            'department': department.pk,
            'level': '400',
            'group': group.pk,
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
        # Two rows, one record: total counts records, repeated explains the gap.
        self.assertEqual(result.rows_read, 2)
        self.assertEqual(result.repeated, 1)
        self.assertEqual(result.total, 1)

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


class PasswordResetTests(TestCase):
    """Django's own flow, so these check the wiring and the wording rather than
    re-testing its token machinery."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='20512001', email='ama@st.knust.edu.gh', password='old-password-1',
        )

    def _link_from_email(self):
        body = mail.outbox[0].body
        match = re.search(r'https?://\S+/password-reset/[^/\s]+/[^/\s]+/', body)
        self.assertIsNotNone(match, f'no reset link in the email:\n{body}')
        return match.group(0)

    def test_every_sign_in_page_offers_the_link(self):
        """One door per audience, and a forgotten password is not one of the
        things that varies between them."""
        for url in ['/student/login/', '/lecturer/login/', '/office/login/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'Forgot your password?')
                self.assertContains(response, '/password-reset/')

    def test_the_request_page_renders(self):
        self.assertEqual(self.client.get('/password-reset/').status_code, 200)

    def test_a_known_address_is_sent_a_link(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('20512001', mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ['ama@st.knust.edu.gh'])

    def test_an_unknown_address_is_answered_the_same_way(self):
        """Otherwise the form tells a stranger which addresses have accounts."""
        known = self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        mail.outbox.clear()
        unknown = self.client.post('/password-reset/', {'email': 'nobody@st.knust.edu.gh'})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known['Location'], unknown['Location'])
        self.assertEqual(len(mail.outbox), 0)

    def test_the_sent_page_does_not_confirm_the_address_exists(self):
        response = self.client.get('/password-reset/sent/')
        self.assertContains(response, 'If an account exists')

    def test_the_link_lets_a_new_password_be_set(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()

        # Django swaps the token for a session-held one and redirects.
        response = self.client.get(link, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Set a new password')

        self.client.post(response.redirect_chain[-1][0], {
            'new_password1': 'a-brand-new-password-9',
            'new_password2': 'a-brand-new-password-9',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('a-brand-new-password-9'))

    def test_the_old_password_stops_working(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()
        response = self.client.get(link, follow=True)
        self.client.post(response.redirect_chain[-1][0], {
            'new_password1': 'a-brand-new-password-9',
            'new_password2': 'a-brand-new-password-9',
        })
        self.assertFalse(self.client.login(username='20512001', password='old-password-1'))
        self.assertTrue(self.client.login(username='20512001', password='a-brand-new-password-9'))

    def test_a_link_cannot_be_used_twice(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()
        response = self.client.get(link, follow=True)
        self.client.post(response.redirect_chain[-1][0], {
            'new_password1': 'a-brand-new-password-9',
            'new_password2': 'a-brand-new-password-9',
        })
        second = self.client.get(link, follow=True)
        self.assertContains(second, 'no longer works')

    def test_a_tampered_link_is_refused(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()
        response = self.client.get(link[:-4] + 'aaa/', follow=True)
        self.assertContains(response, 'no longer works')

    def test_a_weak_password_is_refused(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()
        response = self.client.get(link, follow=True)
        target = response.redirect_chain[-1][0]
        self.client.post(target, {'new_password1': '123', 'new_password2': '123'})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-password-1'))

    def test_mismatched_confirmation_is_refused(self):
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = self._link_from_email()
        response = self.client.get(link, follow=True)
        self.client.post(response.redirect_chain[-1][0], {
            'new_password1': 'a-brand-new-password-9',
            'new_password2': 'a-different-password-9',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-password-1'))

    def test_an_account_with_no_address_gets_nothing(self):
        """Which is why students need an email on their record."""
        User.objects.create_user(username='20512002', password='x')
        self.client.post('/password-reset/', {'email': ''})
        self.assertEqual(len(mail.outbox), 0)

    def test_the_whole_flow_needs_no_sign_in(self):
        for url in ['/password-reset/', '/password-reset/sent/', '/password-reset/done/']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class MyAccountTests(TestCase):
    def setUp(self):
        build_dataset()
        self.student = Student.objects.create(
            student_id='20512001', name='Ama Serwaa', group=StudentGroup.objects.first())
        _user, self.password = create_student_account(self.student)
        self.student.refresh_from_db()
        self.user = self.student.user

    def test_any_signed_in_user_can_reach_it(self):
        for account in (self.user, make_admin('accadmin'),
                        make_linked_lecturer('acclect')):
            with self.subTest(account=account.username):
                self.client.force_login(account)
                self.assertEqual(self.client.get('/account/').status_code, 200)

    def test_it_needs_signing_in(self):
        response = self.client.get('/account/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_setting_an_email_lands_on_both_the_account_and_the_record(self):
        """A reset reads the account; the office reads the record. They must
        not drift apart."""
        self.client.force_login(self.user)
        self.client.post('/account/', {
            'save_email': '1', 'email': 'Ama.Serwaa@st.knust.edu.gh'})
        self.user.refresh_from_db()
        self.student.refresh_from_db()
        self.assertEqual(self.user.email, 'ama.serwaa@st.knust.edu.gh')
        self.assertEqual(self.student.email, 'ama.serwaa@st.knust.edu.gh')

    def test_a_student_can_then_reset_their_own_password(self):
        """The whole point: an account with no address can now get one."""
        self.assertEqual(self.user.email, '')
        self.client.force_login(self.user)
        self.client.post('/account/', {
            'save_email': '1', 'email': 'ama@st.knust.edu.gh'})
        self.client.logout()

        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        self.assertEqual(len(mail.outbox), 1)

    def test_the_page_says_when_there_is_no_address(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/account/'), 'no email address on file')

    def test_an_invalid_address_is_refused(self):
        self.client.force_login(self.user)
        self.client.post('/account/', {'save_email': '1', 'email': 'not-an-email'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, '')

    def test_an_address_can_be_removed(self):
        self.client.force_login(self.user)
        self.client.post('/account/', {'save_email': '1', 'email': 'a@x.gh'})
        self.client.post('/account/', {'save_email': '1', 'email': ''})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, '')

    def test_a_lecturer_cannot_take_another_lecturers_address(self):
        Lecturer.objects.filter(email='l1@example.com').update(email='taken@knust.edu.gh')
        mine = Lecturer.objects.get(email='l0@example.com')
        user = make_lecturer_user('drmine')
        mine.user = user
        mine.save()

        self.client.force_login(user)
        response = self.client.post('/account/', {
            'save_email': '1', 'email': 'taken@knust.edu.gh'})
        self.assertContains(response, 'already uses that address')
        mine.refresh_from_db()
        self.assertEqual(mine.email, 'l0@example.com')

    def test_changing_the_password_works_and_keeps_you_signed_in(self):
        self.client.force_login(self.user)
        response = self.client.post('/account/', {
            'save_password': '1',
            'old_password': self.password,
            'new_password1': 'Timetable-2026-KNUST',
            'new_password2': 'Timetable-2026-KNUST',
        }, follow=True)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Timetable-2026-KNUST'))
        # Still signed in: the session hash was updated with the new password.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/account/').status_code, 200)

    def test_the_current_password_is_required(self):
        self.client.force_login(self.user)
        self.client.post('/account/', {
            'save_password': '1',
            'old_password': 'not-the-right-one',
            'new_password1': 'Timetable-2026-KNUST',
            'new_password2': 'Timetable-2026-KNUST',
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password('Timetable-2026-KNUST'))

    def test_a_weak_new_password_is_refused(self):
        self.client.force_login(self.user)
        self.client.post('/account/', {
            'save_password': '1', 'old_password': self.password,
            'new_password1': '12345678', 'new_password2': '12345678',
        })
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    # --- the preview guard ---------------------------------------------------

    def test_a_preview_cannot_change_the_previewed_password(self):
        """Otherwise View as becomes a way to take over an account."""
        admin = make_admin('previewer')
        self.client.force_login(admin)
        self.client.post(f'/view-as/{self.user.pk}/')

        self.client.post('/account/', {
            'save_password': '1', 'old_password': self.password,
            'new_password1': 'Taken-Over-2026', 'new_password2': 'Taken-Over-2026',
        })
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password('Taken-Over-2026'))
        self.assertTrue(self.user.check_password(self.password))

    def test_a_preview_cannot_change_the_previewed_email(self):
        admin = make_admin('previewer2')
        self.client.force_login(admin)
        self.client.post(f'/view-as/{self.user.pk}/')

        self.client.post('/account/', {'save_email': '1', 'email': 'attacker@x.gh'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, '')

    def test_a_preview_can_still_look_at_the_page(self):
        admin = make_admin('previewer3')
        self.client.force_login(admin)
        self.client.post(f'/view-as/{self.user.pk}/')
        response = self.client.get('/account/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['previewing'])

    def test_the_nav_offers_it_to_everyone(self):
        for account in (self.user, make_admin('navadmin')):
            with self.subTest(account=account.username):
                self.client.force_login(account)
                self.assertContains(self.client.get('/'), 'href="/account/"')


class PasswordToggleTests(TestCase):
    """Every password input gets a reveal control, wired by one shared script."""

    def setUp(self):
        self.student = Student.objects.create(student_id='20512001', name='Ama')
        create_student_account(self.student)
        self.student.refresh_from_db()

    def _reset_confirm_url(self):
        self.student.user.email = 'ama@st.knust.edu.gh'
        self.student.user.save()
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        link = re.search(
            r'https?://[^/]+(/password-reset/[^/\s]+/[^/\s]+/)', mail.outbox[0].body
        ).group(1)
        return self.client.get(link, follow=True).redirect_chain[-1][0]

    def _check(self, body, expected):
        """Each password input must be wrapped and have a toggle beside it."""
        self.assertEqual(body.count('type="password"'), expected)
        self.assertEqual(body.count('class="password-field"'), expected)
        self.assertEqual(body.count('class="password-toggle"'), expected)
        self.assertIn('password-toggle.js', body)

    def test_every_sign_in_door_has_one(self):
        for url in ['/student/login/', '/lecturer/login/', '/office/login/']:
            with self.subTest(url=url):
                self._check(self.client.get(url).content.decode(), 1)

    def test_my_account_has_one_on_each_of_its_three(self):
        self.client.force_login(self.student.user)
        self._check(self.client.get('/account/').content.decode(), 3)

    def test_setting_a_new_password_has_one_on_both(self):
        self._check(self.client.get(self._reset_confirm_url()).content.decode(), 2)

    def test_the_shared_script_is_served(self):
        """Asked of the finders rather than of a URL: a request for it only
        succeeds once collectstatic has run, so going through the URL would
        pass or fail on the state of a build directory rather than on whether
        the file is where the app says it is."""
        self.assertIsNotNone(finders.find('scheduler/password-toggle.js'))

    def test_no_page_ships_its_own_copy_of_the_toggle_script(self):
        """It lived inline in two templates before; a third copy was the prompt
        to factor it out."""
        self.client.force_login(self.student.user)
        for url in ['/login/', '/account/']:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn("input.type = wasRevealed", body)


class EnvironmentSettingTests(SimpleTestCase):
    """A setting read at import time can take the whole site down.

    Adding a key in a hosting dashboard and saving before typing the value is
    an easy thing to do, and clearing a value usually leaves the key in place.
    An empty string is not a number, and the resulting ValueError happens while
    the settings module is still being imported - so it is not a broken
    feature, it is a bare "Internal Server Error" on every page, with nothing
    on screen to connect it to the variable that caused it.
    """

    def _reload_settings(self, **environment):
        """Import the settings module afresh under the given environment."""
        base = {
            'DEBUG': 'False',
            'SECRET_KEY': 'test-only-' + 'x' * 40,
            'ALLOWED_HOSTS': 'example.com',
        }
        base.update(environment)
        with mock.patch.dict(os.environ, base, clear=False):
            for name in environment:
                if environment[name] is None:
                    os.environ.pop(name, None)
            return importlib.reload(importlib.import_module('kts.settings'))

    def tearDown(self):
        # Leave the module holding the values the rest of the suite expects.
        importlib.reload(importlib.import_module('kts.settings'))

    def test_a_blank_number_falls_back_instead_of_felling_the_site(self):
        for name, attribute, default in [
            ('EMAIL_PORT', 'EMAIL_PORT', 587),
            ('PASSWORD_RESET_TIMEOUT', 'PASSWORD_RESET_TIMEOUT', 60 * 60 * 24),
        ]:
            with self.subTest(name=name):
                settings_module = self._reload_settings(**{name: ''})
                self.assertEqual(getattr(settings_module, attribute), default)

    def test_whitespace_around_a_number_is_not_fatal_either(self):
        """Copying a value out of a document brings the spaces with it."""
        self.assertEqual(self._reload_settings(EMAIL_PORT=' 2525 ').EMAIL_PORT, 2525)

    def test_a_value_that_is_genuinely_wrong_says_which_one(self):
        with self.assertRaises(RuntimeError) as caught:
            self._reload_settings(EMAIL_PORT='five eight seven')
        message = str(caught.exception)
        self.assertIn('EMAIL_PORT', message)
        self.assertIn('whole number', message)

    def test_a_blank_flag_keeps_its_default_rather_than_reading_as_false(self):
        """EMAIL_USE_TLS was compared against the string 'True', so a blank
        value quietly turned TLS off - a silent downgrade rather than an
        error, which is worse than the crash."""
        self.assertIs(self._reload_settings(EMAIL_USE_TLS='').EMAIL_USE_TLS, True)

    def test_a_flag_accepts_what_people_actually_type(self):
        for raw, expected in [('true', True), ('True', True), ('1', True),
                              ('false', False), ('False', False), ('0', False),
                              ('no', False), ('YES', True)]:
            with self.subTest(raw=raw):
                self.assertIs(
                    self._reload_settings(EMAIL_USE_TLS=raw).EMAIL_USE_TLS, expected)

    def test_a_flag_that_is_neither_says_which_one(self):
        with self.assertRaises(RuntimeError) as caught:
            self._reload_settings(EMAIL_USE_TLS='maybe')
        self.assertIn('EMAIL_USE_TLS', str(caught.exception))

    def test_a_brevo_key_picks_the_brevo_backend(self):
        for where in ['BREVO_API_KEY', 'EMAIL_HOST_PASSWORD']:
            with self.subTest(where=where):
                reloaded = self._reload_settings(**{where: 'xkeysib-abc'})
                self.assertTrue(reloaded.EMAIL_BACKEND.endswith('BrevoBackend'))

    def test_brevo_wins_when_both_are_configured(self):
        """Only Brevo delivers to a student without a domain being verified, so
        having set it up is the intention that counts."""
        reloaded = self._reload_settings(BREVO_API_KEY='xkeysib-abc',
                                         RESEND_API_KEY='re_abc')
        self.assertTrue(reloaded.EMAIL_BACKEND.endswith('BrevoBackend'))

    def test_brevo_does_not_borrow_resends_sending_address(self):
        """resend.dev works only for Resend. Brevo has no equivalent, so the
        placeholder stands as a marker that the sender is still to be set."""
        reloaded = self._reload_settings(BREVO_API_KEY='xkeysib-abc')
        self.assertNotIn('resend.dev', reloaded.DEFAULT_FROM_EMAIL)
        self.assertTrue(reloaded.MAIL_SENDER_NEEDS_SETTING)

    def test_setting_the_verified_sender_clears_the_marker(self):
        reloaded = self._reload_settings(
            BREVO_API_KEY='xkeysib-abc',
            DEFAULT_FROM_EMAIL='KTS <timetable@gmail.com>')
        self.assertFalse(reloaded.MAIL_SENDER_NEEDS_SETTING)

    def test_resend_gets_a_from_address_it_will_actually_accept(self):
        """A provider only sends from a domain you have proved you own, so the
        placeholder is refused outright - and because a reset swallows delivery
        failures on purpose, refused looks exactly like never sent."""
        reloaded = self._reload_settings(EMAIL_HOST_PASSWORD='re_key')
        self.assertIn('resend.dev', reloaded.DEFAULT_FROM_EMAIL)
        self.assertNotIn('example.com', reloaded.DEFAULT_FROM_EMAIL)

    def test_your_own_from_address_is_left_alone(self):
        reloaded = self._reload_settings(
            EMAIL_HOST_PASSWORD='re_key',
            DEFAULT_FROM_EMAIL='KTS <no-reply@knust.edu.gh>')
        self.assertEqual(reloaded.DEFAULT_FROM_EMAIL, 'KTS <no-reply@knust.edu.gh>')

    def test_a_blank_from_address_does_not_become_the_sender(self):
        """Cleared in a dashboard leaves the key behind with an empty value,
        and an empty from address is refused by every provider there is."""
        self.assertTrue(
            self._reload_settings(DEFAULT_FROM_EMAIL='').DEFAULT_FROM_EMAIL)

    def test_a_mail_host_with_stray_whitespace_still_counts_as_configured(self):
        """A trailing space would otherwise leave EMAIL_HOST truthy but the
        connection pointed at a host that does not resolve."""
        self.assertEqual(
            self._reload_settings(EMAIL_HOST=' smtp.gmail.com ').EMAIL_HOST,
            'smtp.gmail.com')

    def test_asking_for_ssl_turns_off_the_tls_that_is_only_on_by_default(self):
        """Django refuses to build a backend with both set, and that refusal
        arrives as a ValueError at send time - a server error on whichever page
        happened to be sending."""
        reloaded = self._reload_settings(EMAIL_USE_SSL='true')
        self.assertIs(reloaded.EMAIL_USE_SSL, True)
        self.assertIs(reloaded.EMAIL_USE_TLS, False)

    def test_the_two_are_never_both_on(self):
        reloaded = self._reload_settings(EMAIL_USE_SSL='true', EMAIL_USE_TLS='true')
        self.assertFalse(reloaded.EMAIL_USE_SSL and reloaded.EMAIL_USE_TLS)

    def test_kept_connections_are_checked_before_they_are_reused(self):
        """Connections are held between requests to avoid reopening one every
        time. Against a database that suspends itself when idle - which is what
        the free tier of a serverless Postgres does after a few minutes - that
        means picking up a connection that looks fine and is already dead, and
        a server error on whatever page someone happened to arrive at. Only
        ever the first request after a quiet spell, which is why it reads as
        random."""
        database = self._reload_settings().DATABASES['default']
        self.assertTrue(database.get('CONN_HEALTH_CHECKS'),
                        'connections are reused without checking they are alive')

    def test_a_sleeping_database_cannot_hang_the_request(self):
        """Waking a suspended database takes a moment. Waiting on it until the
        host gives up on the whole request helps nobody."""
        database = self._reload_settings(
            DATABASE_URL='postgresql://u:p@example.neon.tech/db').DATABASES['default']
        self.assertLessEqual(database['OPTIONS']['connect_timeout'], 30)

    def test_sqlite_is_not_given_an_option_it_will_reject(self):
        """connect_timeout is a Postgres option; SQLite refuses what it does
        not recognise, which would break every local run."""
        database = self._reload_settings(DATABASE_URL='').DATABASES['default']
        self.assertIn('sqlite', database['ENGINE'])
        self.assertNotIn('connect_timeout', database.get('OPTIONS', {}))

    def test_mail_cannot_hang_longer_than_the_host_will_wait(self):
        """Render gives a request 30 seconds. A mail server that accepts the
        connection and then goes quiet would otherwise hold the worker until
        the operating system gave up, killing the page with no explanation."""
        self.assertLessEqual(self._reload_settings().EMAIL_TIMEOUT, 30)


class MandatoryFieldTests(TestCase):
    """Typing a record in one at a time means having all of it to hand.

    Two of these earn it beyond tidiness: without an index number a student
    cannot set up their own account, and without an email address there is
    nowhere to send the password if they do.
    """

    def setUp(self):
        self.client.force_login(make_admin())
        self.department = Department.objects.get(name='Computer Science')
        self.group = StudentGroup.objects.create(name='CS Level 400')

    def _student_payload(self, **overrides):
        payload = {
            'student_id': '20212007',
            'index_number': '7212007',
            'name': 'Adjoa Mensimah',
            'email': 'adjoa@st.knust.edu.gh',
            'college': self.department.college.pk,
            'department': self.department.pk,
            'level': '400',
            'group': self.group.pk,
        }
        payload.update(overrides)
        return payload

    def test_a_complete_student_is_accepted(self):
        response = self.client.post('/students/add/', self._student_payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Student.objects.filter(student_id='20212007').exists())

    def test_each_required_field_is_actually_required(self):
        for field in ['student_id', 'index_number', 'name', 'email', 'college',
                      'department', 'level']:
            with self.subTest(field=field):
                response = self.client.post(
                    '/students/add/', self._student_payload(**{field: ''}))
                self.assertEqual(response.status_code, 200,
                                 f'{field} was accepted empty')
                self.assertFalse(Student.objects.filter(student_id='20212007').exists())

    def test_the_teaching_group_is_still_optional(self):
        """The one field that may genuinely not be known yet."""
        response = self.client.post('/students/add/', self._student_payload(group=''))
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Student.objects.get(student_id='20212007').group)

    def test_a_lecturer_needs_a_name_and_an_email(self):
        for field in ['name', 'email']:
            with self.subTest(field=field):
                payload = {'name': 'Dr Mensah', 'email': 'm@knust.edu.gh'}
                payload[field] = ''
                response = self.client.post('/lecturers/add/', payload)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Lecturer.objects.exists())

    def test_the_required_fields_are_marked_on_screen(self):
        """A field that will not be accepted empty should say so before it is
        submitted, not after."""
        body = self.client.get('/students/add/').content.decode()
        self.assertEqual(body.count('class="required-mark"'), 7)

        # Lecturer ID, name, email. The login account is not one of them.
        lecturer_page = self.client.get('/lecturers/add/').content.decode()
        self.assertEqual(lecturer_page.count('class="required-mark"'), 3)

    def test_the_optional_ones_are_not_marked(self):
        body = self.client.get('/students/add/').content.decode()
        group_label = body.split('Student group')[1][:120]
        self.assertNotIn('required-mark', group_label)
        self.assertIn('(optional)', group_label)

    def test_the_browser_is_told_too(self):
        """The asterisk is decoration. This is what a screen reader announces,
        and what stops an empty form reaching the server at all."""
        body = self.client.get('/students/add/').content.decode()
        self.assertIn('required', body)
        for field in ['id_college', 'id_department']:
            with self.subTest(field=field):
                markup = body.split(field)[1][:200]
                self.assertIn('required', markup)

    def test_a_lecturers_login_account_is_still_optional(self):
        """It is a link to something that may not exist yet, not a fact about
        the lecturer - and a lecturer can now make one themselves anyway."""
        response = self.client.post('/lecturers/add/', {
            'lecturer_id': 'KNUST/CS/014',
            'name': 'Dr Mensah',
            'email': 'm@knust.edu.gh',
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Lecturer.objects.get().user)

    def test_the_import_is_not_held_to_the_same_standard(self):
        """A file from the faculty office arrives with whatever it arrives
        with. Rejecting hundreds of rows over a missing index number would be
        worse than holding them."""
        result = run_import('students', csv_upload(
            'student_id,name\n20212099,Kofi Owusu\n'))
        self.assertEqual(result.created, 1)
        student = Student.objects.get(student_id='20212099')
        self.assertIsNone(student.index_number)
        self.assertEqual(student.email, '')


class SearchSuggestionTests(TestCase):
    """Records matching what has been typed, under the search box."""

    def setUp(self):
        build_dataset()
        self.client.force_login(make_admin())
        self.student = Student.objects.create(
            student_id='20212007', index_number='7212007', name='Adjoa Mensimah',
            department=Department.objects.get(name='Computer Science'), level='400')

    def _suggest(self, kind, term):
        response = self.client.get(f'/suggest/{kind}/', {'q': term})
        self.assertEqual(response.status_code, 200)
        return response.json()['results']

    def test_it_finds_a_student_by_name(self):
        results = self._suggest('students', 'Adjoa')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['label'], 'Adjoa Mensimah')
        self.assertIn('20212007', results[0]['detail'])
        self.assertEqual(results[0]['url'], f'/students/{self.student.pk}/edit/')

    def test_it_finds_a_student_by_the_numbers_too(self):
        for term in ['20212007', '7212007']:
            with self.subTest(term=term):
                self.assertEqual(len(self._suggest('students', term)), 1)

    def test_one_letter_is_enough(self):
        """Looking someone up starts with knowing how their name begins."""
        Student.objects.create(student_id='20212010', name='Akosua Boateng')
        results = self._suggest('students', 'a')
        self.assertIn('Akosua Boateng', [r['label'] for r in results])

    def test_names_starting_with_the_letter_come_first(self):
        Student.objects.create(student_id='20212010', name='Akosua Boateng')
        Student.objects.create(student_id='20212011', name='Kofi Amankwah')
        Student.objects.create(student_id='20212012', name='Yaw Danso')

        labels = [r['label'] for r in self._suggest('students', 'a')]
        # Starts with A, then has a word starting with A, then merely contains
        # one - "Yaw Danso" has two, neither of them at the front of anything.
        self.assertLess(labels.index('Akosua Boateng'), labels.index('Kofi Amankwah'))
        self.assertLess(labels.index('Kofi Amankwah'), labels.index('Yaw Danso'))

    def test_the_order_is_stable_as_more_letters_arrive(self):
        """A list that reshuffles under the pointer is worse than a slow one."""
        for i, name in enumerate(['Ama Serwaa', 'Ama Boateng', 'Ama Darko']):
            Student.objects.create(student_id=f'2021301{i}', name=name)
        first = [r['label'] for r in self._suggest('students', 'am')]
        second = [r['label'] for r in self._suggest('students', 'ama')]
        self.assertEqual(first, second)

    def test_a_title_is_not_treated_as_the_name(self):
        """Almost every lecturer's name starts with one, so left in place
        searching "m" offers every Mr and Ms before Mensah."""
        Lecturer.objects.create(name='Dr. Kwame Mensah', email='kmensah@knust.edu.gh')
        Lecturer.objects.create(name='Mr. Kofi Danso', email='kdanso@knust.edu.gh')

        labels = [r['label'] for r in self._suggest('lecturers', 'm')]
        self.assertLess(labels.index('Dr. Kwame Mensah'),
                        labels.index('Mr. Kofi Danso'))

    def test_an_empty_box_suggests_nothing(self):
        self.assertEqual(self._suggest('students', ''), [])

    def test_it_offers_what_pressing_enter_would_find(self):
        """The dropdown and the page search read the same field list, so a
        suggestion cannot promise something the search will not return."""
        term = 'Adjoa'
        suggested = {r['label'] for r in self._suggest('students', term)}
        listed = {s.name for s in
                  self.client.get('/students/', {'q': term}).context['students']}
        self.assertEqual(suggested, listed)

    def test_every_searchable_page_can_suggest(self):
        for kind in ['lecturers', 'courses', 'rooms', 'groups', 'students',
                     'colleges', 'departments']:
            with self.subTest(kind=kind):
                response = self.client.get(f'/suggest/{kind}/', {'q': 'co'})
                self.assertEqual(response.status_code, 200)

    def test_the_list_pages_ask_for_suggestions(self):
        pages = {
            '/lecturers/': 'lecturers', '/courses/': 'courses',
            '/rooms/': 'rooms', '/student-groups/': 'groups',
            '/students/': 'students', '/colleges/': 'colleges',
            '/departments/': 'departments',
        }
        for url, kind in pages.items():
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn(f'/suggest/{kind}/', body)
                self.assertIn('search-suggest.js', body)

    def test_it_never_offers_more_than_it_promises(self):
        for i in range(20):
            Student.objects.create(student_id=f'2021{i:04d}', name=f'Kofi Test {i}')
        self.assertLessEqual(len(self._suggest('students', 'Kofi')), 8)

    def test_an_unknown_kind_is_not_a_search(self):
        self.assertEqual(self.client.get('/suggest/passwords/', {'q': 'ab'}).status_code,
                         404)

    def test_it_is_not_open_to_anyone_signed_out(self):
        """The list pages are administrator-only, and this returns the same
        records - it would be a way around them otherwise."""
        self.client.logout()
        response = self.client.get('/suggest/students/', {'q': 'Adjoa'})
        self.assertNotEqual(response.status_code, 200)

    def test_a_student_cannot_use_it_to_read_the_roster(self):
        create_student_account(self.student)
        self.student.refresh_from_db()
        self.client.force_login(self.student.user)
        response = self.client.get('/suggest/students/', {'q': 'Adjoa'})
        self.assertNotEqual(response.status_code, 200)


class CollegeDepartmentTests(TestCase):
    """Colleges and departments are records, not a list in the code.

    A department is also what a student's programme is, so the two have to
    stay one answer: renaming a department renames the programme of everyone
    on it.
    """

    def setUp(self):
        self.client.force_login(make_admin())
        self.science = College.objects.get(name='College of Science')
        self.cs = Department.objects.get(name='Computer Science',
                                         college=self.science)

    def test_the_migration_seeded_the_university(self):
        """A student cannot set up an account with nothing to choose from."""
        self.assertEqual(College.objects.count(), 6)
        self.assertTrue(Department.objects.filter(name='Computer Science').exists())

    def test_the_pages_are_reachable(self):
        for url in ['/colleges/', '/colleges/add/', '/departments/',
                    '/departments/add/',
                    f'/colleges/{self.science.pk}/edit/',
                    f'/departments/{self.cs.pk}/edit/',
                    f'/colleges/{self.science.pk}/delete/',
                    f'/departments/{self.cs.pk}/delete/']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_they_are_in_the_sidebar(self):
        body = self.client.get('/').content.decode()
        self.assertIn('/colleges/', body)
        self.assertIn('/departments/', body)

    def test_a_student_cannot_reach_them(self):
        student = Student.objects.create(student_id='20512001', name='Ama')
        create_student_account(student)
        student.refresh_from_db()
        self.client.force_login(student.user)
        for url in ['/colleges/', '/departments/']:
            with self.subTest(url=url):
                self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_renaming_a_department_renames_the_programme_of_its_students(self):
        """Otherwise the students page says one thing and the student's own
        account says another."""
        student = Student.objects.create(
            student_id='20512001', name='Ama', department=self.cs)
        student.refresh_from_db()
        self.assertEqual(student.programme, 'Computer Science')

        self.client.post(f'/departments/{self.cs.pk}/edit/', {
            'name': 'Computer Science and Engineering',
            'college': self.science.pk,
        })

        student.refresh_from_db()
        self.assertEqual(student.programme, 'Computer Science and Engineering')

    def test_moving_a_department_moves_its_students_college(self):
        engineering = College.objects.get(name='College of Engineering')
        student = Student.objects.create(
            student_id='20512001', name='Ama', department=self.cs)

        self.client.post(f'/departments/{self.cs.pk}/edit/', {
            'name': 'Computer Science', 'college': engineering.pk,
        })

        student.refresh_from_db()
        self.assertEqual(student.college, engineering)

    def test_two_colleges_may_each_have_a_department_of_the_same_name(self):
        engineering = College.objects.get(name='College of Engineering')
        Department.objects.create(name='Mathematics', college=engineering)
        self.assertEqual(Department.objects.filter(name='Mathematics').count(), 2)

    def test_one_college_may_not_have_two(self):
        with self.assertRaises(Exception):
            Department.objects.create(name='Computer Science', college=self.science)

    def test_deleting_a_department_leaves_the_students_programme_alone(self):
        """The roster said what they study. Losing the department here is our
        problem, not a reason to forget it."""
        student = Student.objects.create(
            student_id='20512001', name='Ama', department=self.cs)
        self.client.post(f'/departments/{self.cs.pk}/delete/')

        student.refresh_from_db()
        self.assertIsNone(student.department)
        self.assertEqual(student.programme, 'Computer Science')


class NotificationPlacementTests(TestCase):
    """Notifications sit with the account they belong to, not with the
    sections of the app."""

    def setUp(self):
        self.admin = make_admin()
        self.client.force_login(self.admin)

    def test_the_bell_is_in_the_topbar(self):
        body = self.client.get('/').content.decode()
        self.assertIn('topbar-bell', body)
        # And no longer among the sidebar's sections.
        sidebar = body.split('<div class="main-wrap">')[0]
        self.assertNotIn('/notifications/', sidebar)

    def test_it_counts_only_what_is_unread(self):
        Notification.objects.create(user=self.admin, message='one')
        Notification.objects.create(user=self.admin, message='two',
                                    read_at=timezone.now())
        body = self.client.get('/').content.decode()
        self.assertIn('class="bell-count"', body)
        self.assertIn('>1<', body)

    def test_nothing_waiting_shows_no_number(self):
        """An empty bell is quieter than a zero.

        Matched on the attribute rather than the bare word: the class is
        defined in the stylesheet, which is on every page, so the bare word
        would be found whatever the markup did.
        """
        body = self.client.get('/').content.decode()
        self.assertNotIn('class="bell-count"', body)

    def test_the_page_is_still_there(self):
        self.assertEqual(self.client.get('/notifications/').status_code, 200)


class SignInDoorTests(TestCase):
    """One sign-in page per audience.

    The doors are not a security measure - anyone can find the other two - so
    nothing here depends on them being secret. What they have to do is be
    written for whoever arrives, and refuse an account that belongs somewhere
    else rather than dropping it on a dashboard built for someone different.
    """

    def setUp(self):
        self.admin = make_admin()
        self.admin.set_password('office-pass-9931')
        self.admin.save()

        self.lecturer = Lecturer.objects.create(name='Dr Mensah', email='m@knust.edu.gh')
        self.lecturer.user = User.objects.create_user('mensah', 'm@knust.edu.gh',
                                                      'lecturer-pass-9931')
        self.lecturer.save()

        self.student = Student.objects.create(
            student_id='20512001', index_number='7512001', name='Ama',
            email='ama@st.knust.edu.gh')
        create_student_account(self.student)
        self.student.refresh_from_db()
        self.student.user.set_password('student-pass-9931')
        self.student.user.save()

    def test_the_chooser_offers_all_three(self):
        body = self.client.get('/login/').content.decode()
        for url in ['/student/login/', '/lecturer/login/', '/office/login/']:
            with self.subTest(url=url):
                self.assertIn(url, body)

    def test_each_door_speaks_to_its_own_audience(self):
        """A lecturer has no student ID, so asking for one is worse than
        useless - it suggests they are in the wrong place."""
        student_page = self.client.get('/student/login/').content.decode()
        self.assertIn('Student ID or email', student_page)

        office_page = self.client.get('/office/login/').content.decode()
        self.assertNotIn('Student ID or email', office_page)

    def _sign_in(self, door, username, password):
        return self.client.post(door, {'username': username, 'password': password})

    def test_each_role_gets_in_at_its_own_door(self):
        cases = [
            ('/student/login/', '20512001', 'student-pass-9931'),
            ('/lecturer/login/', 'mensah', 'lecturer-pass-9931'),
            ('/office/login/', self.admin.username, 'office-pass-9931'),
        ]
        for door, username, password in cases:
            with self.subTest(door=door):
                self.client.logout()
                response = self._sign_in(door, username, password)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, '/')

    def test_the_wrong_door_sends_you_to_the_right_one(self):
        """Refusing outright would be unhelpful, and letting them through
        would land a student on the timetable office's dashboard."""
        cases = [
            ('/office/login/', '20512001', 'student-pass-9931', '/student/login/'),
            ('/student/login/', self.admin.username, 'office-pass-9931', '/office/login/'),
            ('/student/login/', 'mensah', 'lecturer-pass-9931', '/lecturer/login/'),
        ]
        for door, username, password, expected in cases:
            with self.subTest(door=door, username=username):
                self.client.logout()
                response = self._sign_in(door, username, password)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, expected)

    def test_the_wrong_door_does_not_leave_you_signed_in(self):
        """Being bounced has to mean not signed in, or the redirect is a
        cosmetic detour around the check it exists to make."""
        self._sign_in('/office/login/', '20512001', 'student-pass-9931')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_an_account_with_no_role_is_told_so(self):
        User.objects.create_user('nobody', 'n@e.com', 'nobody-pass-9931')
        response = self._sign_in('/student/login/', 'nobody', 'nobody-pass-9931')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no role yet', response.content.decode())

    def test_signing_in_with_an_email_instead(self):
        response = self._sign_in('/student/login/', 'ama@st.knust.edu.gh',
                                 'student-pass-9931')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/')

    def test_an_email_shared_by_two_accounts_signs_in_neither(self):
        """Guessing which of two people meant to sign in is not something to
        do quietly."""
        User.objects.create_user('twin', 'ama@st.knust.edu.gh', 'student-pass-9931')
        response = self._sign_in('/student/login/', 'ama@st.knust.edu.gh',
                                 'student-pass-9931')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_the_chooser_carries_where_you_were_going(self):
        """login_required sends people here mid-journey."""
        body = self.client.get('/login/?next=/timetable/').content.decode()
        self.assertIn('next=%2Ftimetable%2F', body)

    def test_a_protected_page_still_sends_you_to_sign_in(self):
        response = self.client.get('/timetable/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)


class StudentActivationTests(TestCase):
    """A student claiming the record the timetable office already holds.

    Nothing is created that was not on the roster first. The claim check is
    the security: student IDs run in sequence, so one can be guessed from a
    classmate's, and this ends with a password being emailed to whatever
    address was typed in.
    """

    def setUp(self):
        self.college = College.objects.get(name='College of Science')
        self.department = Department.objects.get(name='Computer Science',
                                                 college=self.college)
        self.student = Student.objects.create(
            student_id='20212099', index_number='7212099', name='Yaa Boateng')

    def _activate(self, **overrides):
        payload = {
            'college': self.college.pk,
            'department': self.department.pk,
            'student_id': '20212099',
            'index_number': '7212099',
            'email': 'yaa@st.knust.edu.gh',
        }
        payload.update(overrides)
        return self.client.post('/student/activate/', payload)

    def test_it_attaches_an_account_to_the_roster_record(self):
        response = self._activate()
        self.assertEqual(response.status_code, 200)

        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.user)
        self.assertEqual(self.student.user.username, '20212099')
        self.assertEqual(self.student.email, 'yaa@st.knust.edu.gh')
        self.assertEqual(self.student.department, self.department)
        self.assertEqual(self.student.college, self.college)

    def test_the_programme_follows_the_department(self):
        self._activate()
        self.student.refresh_from_db()
        self.assertEqual(self.student.programme, 'Computer Science')

    def test_the_password_is_emailed_to_them(self):
        self._activate()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['yaa@st.knust.edu.gh'])
        self.assertIn('20212099', mail.outbox[0].body)

    def test_the_emailed_password_is_the_one_that_works(self):
        self._activate()
        password = re.search(r'Password:\s+(\S+)', mail.outbox[0].body).group(1)
        self.student.refresh_from_db()
        self.assertTrue(self.student.user.check_password(password))

    def test_a_guessed_student_id_gets_nowhere_without_the_index_number(self):
        """The whole point of asking for two numbers."""
        response = self._activate(index_number='0000000',
                                  email='attacker@example.com')
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertIsNone(self.student.user)
        self.assertEqual(len(mail.outbox), 0)
        self.assertNotEqual(self.student.email, 'attacker@example.com')

    def test_an_unknown_student_id_is_refused_the_same_way(self):
        """Telling the two apart would turn this into a way of discovering
        which student IDs exist, one guess at a time."""
        unknown = self._activate(student_id='99999999')
        wrong_index = self._activate(index_number='0000000')
        self.assertEqual(
            re.findall(r'could not match those details', unknown.content.decode()),
            re.findall(r'could not match those details', wrong_index.content.decode()),
        )

    def test_a_student_who_already_has_an_account_is_sent_to_sign_in(self):
        self._activate()
        mail.outbox.clear()
        response = self._activate()
        self.assertIn('already has an account', response.content.decode())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_department_from_another_college_is_refused(self):
        other = College.objects.exclude(pk=self.college.pk).first()
        response = self._activate(college=other.pk)
        self.assertIn('not in that college', response.content.decode())
        self.student.refresh_from_db()
        self.assertIsNone(self.student.user)

    def test_the_account_survives_the_email_failing(self):
        """The account exists by then. Sending them back to the form would
        only tell them the record is already taken."""
        with mock.patch('scheduler.auth_views.send_mail',
                        side_effect=OSError('mail server down')):
            response = self._activate()
        self.student.refresh_from_db()
        self.assertIsNotNone(self.student.user)
        self.assertIn('did not send', response.content.decode())

    def test_it_is_reachable_from_the_student_door(self):
        self.assertIn('/student/activate/',
                      self.client.get('/student/login/').content.decode())


class LecturerActivationTests(TestCase):
    """A lecturer claiming the record the timetable office already holds.

    The pair is the lecturer ID and the address already on file. That the
    address is checked rather than supplied is what makes this safe without a
    third factor: the password goes where the timetable office recorded it, so
    guessing an ID puts the mail in the real lecturer's inbox.
    """

    def setUp(self):
        self.lecturer = Lecturer.objects.create(
            lecturer_id='KNUST/CS/014', name='Dr. Kwame Mensah',
            email='kmensah@knust.edu.gh')

    def _activate(self, **overrides):
        payload = {'lecturer_id': 'KNUST/CS/014', 'email': 'kmensah@knust.edu.gh'}
        payload.update(overrides)
        return self.client.post('/lecturer/activate/', payload)

    def test_it_attaches_an_account_to_the_roster_record(self):
        response = self._activate()
        self.assertEqual(response.status_code, 200)

        self.lecturer.refresh_from_db()
        self.assertIsNotNone(self.lecturer.user)
        self.assertEqual(self.lecturer.user.username, 'KNUST/CS/014')
        self.assertEqual(self.lecturer.user.email, 'kmensah@knust.edu.gh')

    def test_the_password_goes_to_the_address_on_file(self):
        """Not to one the form supplied - there is no such field."""
        self._activate()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['kmensah@knust.edu.gh'])

    def test_the_emailed_password_is_the_one_that_works(self):
        self._activate()
        password = re.search(r'Password:\s+(\S+)', mail.outbox[0].body).group(1)
        self.lecturer.refresh_from_db()
        self.assertTrue(self.lecturer.user.check_password(password))

    def test_a_guessed_id_with_a_different_address_gets_nowhere(self):
        response = self._activate(email='attacker@example.com')
        self.assertEqual(response.status_code, 200)
        self.lecturer.refresh_from_db()
        self.assertIsNone(self.lecturer.user)
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unknown_id_is_refused_the_same_way(self):
        """Telling the two apart would turn this into a way of working out who
        is on staff, and then of harvesting their addresses."""
        unknown = self._activate(lecturer_id='KNUST/CS/999')
        wrong_email = self._activate(email='someone@else.com')
        needle = 'could not match those details'
        self.assertIn(needle, unknown.content.decode())
        self.assertIn(needle, wrong_email.content.decode())

    def test_the_address_is_matched_whatever_the_case(self):
        response = self._activate(email='KMensah@KNUST.edu.gh')
        self.lecturer.refresh_from_db()
        self.assertIsNotNone(self.lecturer.user, response.content.decode()[:400])

    def test_a_lecturer_who_already_has_an_account_is_sent_to_sign_in(self):
        self._activate()
        mail.outbox.clear()
        response = self._activate()
        self.assertIn('already has an account', response.content.decode())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_lecturer_with_no_id_yet_cannot_be_claimed(self):
        """The ones who predate the column. Until the timetable office gives
        them an ID there is nothing to prove with, and a blank must not match
        a blank."""
        Lecturer.objects.create(name='Dr. Efua Sarpong', email='esarpong@knust.edu.gh')
        response = self.client.post('/lecturer/activate/', {
            'lecturer_id': '', 'email': 'esarpong@knust.edu.gh'})
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(Lecturer.objects.get(email='esarpong@knust.edu.gh').user)

    def test_they_can_sign_in_by_id_or_by_email(self):
        self._activate()
        password = re.search(r'Password:\s+(\S+)', mail.outbox[0].body).group(1)
        for who in ['KNUST/CS/014', 'kmensah@knust.edu.gh']:
            with self.subTest(who=who):
                self.client.logout()
                response = self.client.post(
                    '/lecturer/login/', {'username': who, 'password': password})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, '/')

    def test_the_account_survives_the_email_failing(self):
        with mock.patch('scheduler.auth_views.send_mail',
                        side_effect=OSError('mail server down')):
            response = self._activate()
        self.lecturer.refresh_from_db()
        self.assertIsNotNone(self.lecturer.user)
        self.assertIn('did not send', response.content.decode())

    def test_it_is_reachable_from_the_lecturer_door(self):
        self.assertIn('/lecturer/activate/',
                      self.client.get('/lecturer/login/').content.decode())

    def test_two_lecturers_cannot_share_an_id(self):
        with self.assertRaises(Exception):
            Lecturer.objects.create(
                lecturer_id='KNUST/CS/014', name='Someone Else',
                email='else@knust.edu.gh')

    def test_several_may_have_none(self):
        """Nullable, not blank: two empty strings would collide where two
        NULLs do not, and most of the roster has no ID yet."""
        Lecturer.objects.create(name='A', email='a@knust.edu.gh')
        Lecturer.objects.create(name='B', email='b@knust.edu.gh')
        self.assertEqual(Lecturer.objects.filter(lecturer_id__isnull=True).count(), 2)


class LecturerIdAdminTests(TestCase):
    """The timetable office side of the staff number."""

    def setUp(self):
        self.client.force_login(make_admin())

    def test_the_form_insists_on_one(self):
        """A lecturer without an ID cannot set up their own account, which is
        the whole point of collecting it."""
        response = self.client.post("/lecturers/add/", {
            "name": "Dr. Kwame Mensah", "email": "kmensah@knust.edu.gh",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Lecturer.objects.filter(email="kmensah@knust.edu.gh").exists())

    def test_a_complete_record_saves(self):
        response = self.client.post("/lecturers/add/", {
            "lecturer_id": "KNUST/CS/014", "name": "Dr. Kwame Mensah",
            "email": "kmensah@knust.edu.gh",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Lecturer.objects.get(email="kmensah@knust.edu.gh").lecturer_id,
            "KNUST/CS/014")

    def test_it_is_marked_required_on_screen(self):
        body = self.client.get("/lecturers/add/").content.decode()
        self.assertEqual(body.count('class="required-mark"'), 3)

    def test_the_list_shows_it(self):
        Lecturer.objects.create(lecturer_id="KNUST/CS/014",
                                name="Dr. Kwame Mensah",
                                email="kmensah@knust.edu.gh")
        body = self.client.get("/lecturers/").content.decode()
        self.assertIn("KNUST/CS/014", body)
        self.assertIn('data-label="Lecturer ID"', body)

    def test_a_lecturer_without_one_is_shown_as_missing_not_blank(self):
        """The ones who predate the column. A blank cell reads as a rendering
        fault; a dash reads as nothing on file."""
        Lecturer.objects.create(name="Dr. Efua Sarpong",
                                email="esarpong@knust.edu.gh")
        body = self.client.get("/lecturers/").content.decode()
        self.assertIn("&mdash;", body)

    def test_it_is_searchable(self):
        Lecturer.objects.create(lecturer_id="KNUST/CS/014",
                                name="Dr. Kwame Mensah",
                                email="kmensah@knust.edu.gh")
        Lecturer.objects.create(lecturer_id="KNUST/MA/002",
                                name="Dr. Efua Sarpong",
                                email="esarpong@knust.edu.gh")
        body = self.client.get("/lecturers/?q=KNUST/CS").content.decode()
        self.assertIn("Dr. Kwame Mensah", body)
        self.assertNotIn("Dr. Efua Sarpong", body)


class LecturerIdImportTests(TestCase):
    """The column has to arrive in bulk, or the whole faculty is typed in."""

    def test_it_imports(self):
        result = run_import("lecturers", csv_upload(
            "lecturer_id,name,email\n"
            "KNUST/CS/014,Dr. Kwame Mensah,kmensah@knust.edu.gh\n"))
        self.assertEqual(result.created, 1)
        self.assertEqual(Lecturer.objects.get().lecturer_id, "KNUST/CS/014")

    def test_a_file_without_the_column_still_imports(self):
        """It is new, and the files already sent do not have it."""
        result = run_import("lecturers", csv_upload(
            "name,email\nDr. Kwame Mensah,kmensah@knust.edu.gh\n"))
        self.assertEqual(result.created, 1)
        self.assertIsNone(Lecturer.objects.get().lecturer_id)

    def test_a_staff_number_column_is_recognised(self):
        run_import("lecturers", csv_upload(
            "Staff Number,Name,Email\n"
            "KNUST/CS/014,Dr. Kwame Mensah,kmensah@knust.edu.gh\n"))
        self.assertEqual(Lecturer.objects.get().lecturer_id, "KNUST/CS/014")

    def test_a_blank_cell_does_not_erase_the_id_on_file(self):
        Lecturer.objects.create(lecturer_id="KNUST/CS/014",
                                name="Dr. Kwame Mensah",
                                email="kmensah@knust.edu.gh")
        run_import("lecturers", csv_upload(
            "lecturer_id,name,email\n,Dr. Kwame Mensah,kmensah@knust.edu.gh\n"))
        self.assertEqual(Lecturer.objects.get().lecturer_id, "KNUST/CS/014")

    def test_an_id_belonging_to_someone_else_is_reported_not_swallowed(self):
        """The rest of the record is still worth having, so only the clashing
        ID is dropped - and the clash is said out loud."""
        Lecturer.objects.create(lecturer_id="KNUST/CS/014",
                                name="Dr. Kwame Mensah",
                                email="kmensah@knust.edu.gh")
        result = run_import("lecturers", csv_upload(
            "lecturer_id,name,email\n"
            "KNUST/CS/014,Dr. Efua Sarpong,esarpong@knust.edu.gh\n"))
        self.assertEqual(result.created, 1)
        self.assertTrue(result.skipped)
        self.assertIsNone(
            Lecturer.objects.get(email="esarpong@knust.edu.gh").lecturer_id)

    def test_the_template_offered_for_download_has_the_column(self):
        self.assertEqual(
            template_csv("lecturers").splitlines()[0].split(","),
            ["lecturer_id", "name", "email"])


class SecondSubmissionTests(TestCase):
    """Pressing "Set password" twice told people the opposite of what happened.

    A reset link is spent the moment the first submission succeeds. A double
    tap - or a second press because a sleeping host made the first look
    ignored - arrives at a token that is already gone, and the page for an
    expired link is what comes back. The password had been changed. The person
    was told to go and request another link, which could only undo it.
    """

    def setUp(self):
        self.student = Student.objects.create(
            student_id='20512099', name='Ama', email='ama@st.knust.edu.gh')
        create_student_account(self.student)
        self.student.refresh_from_db()
        self.user = self.student.user
        self.new_password = 'Kumasi!2026pass'

    def _form_url(self):
        self.client.post('/password-reset/', {'email': self.user.email})
        link = re.search(r'https?://[^/]+(/password-reset/[^/\s]+/[^/\s]+/)',
                         mail.outbox[0].body).group(1)
        return self.client.get(link, follow=True).redirect_chain[-1][0]

    def _submit(self, url):
        return self.client.post(url, {'new_password1': self.new_password,
                                      'new_password2': self.new_password})

    def test_the_first_submission_works(self):
        response = self._submit(self._form_url())
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))

    def test_a_second_submission_does_not_undo_the_first(self):
        """It cannot be made to succeed - the token is genuinely spent - so
        what matters is that the password stays changed."""
        url = self._form_url()
        self._submit(url)
        self._submit(url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))

    def test_the_expired_page_tells_them_to_try_signing_in_first(self):
        """The page they land on is the one for an expired link, and the
        obvious advice on it - request another link - is the one instruction
        that cannot help someone whose password has just been set."""
        url = self._form_url()
        self._submit(url)
        body = self._submit(url).content.decode()

        self.assertIn('it worked', body)
        self.assertIn('Try', body)
        self.assertIn('signing in', body)

    def test_the_form_guards_against_the_second_press(self):
        """The wording is the safety net. This is the fix: the browser is told
        not to send the second submission at all."""
        body = self.client.get(self._form_url()).content.decode()
        self.assertIn('data-busy', body)
        self.assertIn('form-busy.js', body)

    def test_every_form_that_can_be_pressed_twice_is_guarded(self):
        """Signing in, asking for a link, setting a password, and the three on
        My Account - all of them slow enough on a sleeping host to invite a
        second press."""
        self.user.email = 'ama@st.knust.edu.gh'
        self.user.save()

        pages = ['/student/login/', '/lecturer/login/', '/office/login/',
                 '/student/activate/', '/password-reset/', self._form_url()]
        for url in pages:
            with self.subTest(url=url):
                self.assertIn('data-busy', self.client.get(url).content.decode())

        self.client.force_login(self.user)
        account = self.client.get('/account/').content.decode()
        self.assertEqual(account.count('<form method="post" data-busy>'), 2,
                         'the email and password forms should both be guarded')


class BrevoBackendTests(SimpleTestCase):
    """The provider that makes self-service resets possible without a domain.

    Resend will not deliver to anyone but the account holder until a whole
    domain is verified through DNS, which needs a domain to own. Brevo verifies
    a single address you already have, so a student who forgets their password
    can get a link.
    """

    def _backend(self, **kwargs):
        kwargs.setdefault('api_key', 'xkeysib-test')
        return BrevoBackend(**kwargs)

    def _message(self):
        return EmailMessage(
            subject='KTS password reset',
            body='Follow this link: https://kts.example/password-reset/a/b/',
            from_email='KNUST Timetable System <timetable@gmail.com>',
            to=['student@st.knust.edu.gh'],
        )

    def _posted(self, mocked):
        return mocked.call_args[0][0]

    def test_it_posts_the_message_the_way_brevo_expects(self):
        with mock.patch('scheduler.mail.urllib.request.urlopen') as urlopen:
            self.assertEqual(self._backend().send_messages([self._message()]), 1)
        request = self._posted(urlopen)
        self.assertTrue(request.full_url.startswith('https://'))
        # Brevo authenticates on its own header, not a bearer token.
        self.assertEqual(request.get_header('Api-key'), 'xkeysib-test')

        payload = json.loads(request.data.decode())
        self.assertEqual(payload['to'], [{'email': 'student@st.knust.edu.gh'}])
        self.assertEqual(payload['subject'], 'KTS password reset')
        self.assertIn('password-reset', payload['textContent'])

    def test_the_sender_is_split_into_the_two_parts_brevo_wants(self):
        """It takes a name and an address separately, not one run together."""
        with mock.patch('scheduler.mail.urllib.request.urlopen') as urlopen:
            self._backend().send_messages([self._message()])
        sender = json.loads(self._posted(urlopen).data.decode())['sender']
        self.assertEqual(sender['email'], 'timetable@gmail.com')
        self.assertEqual(sender['name'], 'KNUST Timetable System')

    def _refusing_with(self, code, message):
        return urllib.error.HTTPError(
            'https://api.brevo.com/v3/smtp/email', code, 'error', {},
            io.BytesIO(json.dumps({'message': message}).encode()))

    def test_an_unverified_sender_says_what_to_do_about_it(self):
        """The one failure this provider is actually likely to hit."""
        with mock.patch('scheduler.mail.urllib.request.urlopen',
                        side_effect=self._refusing_with(
                            400, 'Sender email is not valid')):
            with self.assertRaises(MailDeliveryError) as caught:
                self._backend().send_messages([self._message()])
        message = str(caught.exception)
        self.assertIn('has not been verified', message)
        self.assertIn('DEFAULT_FROM_EMAIL', message)

    def test_other_refusals_are_reported_too(self):
        for code, advice in [(401, 'BREVO_API_KEY'),
                             (402, 'daily limit'),
                             (429, 'Wait a moment')]:
            with self.subTest(code=code):
                with mock.patch('scheduler.mail.urllib.request.urlopen',
                                side_effect=self._refusing_with(code, 'detail')):
                    with self.assertRaises(MailDeliveryError) as caught:
                        self._backend().send_messages([self._message()])
                self.assertIn(advice, str(caught.exception))

    def test_a_missing_key_says_so(self):
        with self.assertRaises(MailDeliveryError) as caught:
            self._backend(api_key='').send_messages([self._message()])
        self.assertIn('BREVO_API_KEY', str(caught.exception))

    def test_it_stays_quiet_when_asked_to(self):
        """A password reset sends with fail_silently, and must not become a
        server error because mail is misconfigured."""
        self.assertEqual(
            self._backend(api_key='', fail_silently=True)
            .send_messages([self._message()]), 0)

    def test_the_key_is_found_where_brevos_own_instructions_put_it(self):
        with override_settings(BREVO_API_KEY='', EMAIL_HOST_PASSWORD='xkeysib-smtp'):
            self.assertEqual(brevo_api_key(), 'xkeysib-smtp')

    def test_an_ordinary_smtp_password_is_not_mistaken_for_a_key(self):
        with override_settings(BREVO_API_KEY='', EMAIL_HOST_PASSWORD='hunter2'):
            self.assertEqual(brevo_api_key(), '')

    def test_the_two_providers_do_not_claim_each_others_keys(self):
        """Both read EMAIL_HOST_PASSWORD, and only one of them should answer."""
        with override_settings(BREVO_API_KEY='', RESEND_API_KEY='',
                               EMAIL_HOST_PASSWORD='xkeysib-brevo'):
            self.assertEqual(brevo_api_key(), 'xkeysib-brevo')
            self.assertEqual(resend_api_key(), '')
        with override_settings(BREVO_API_KEY='', RESEND_API_KEY='',
                               EMAIL_HOST_PASSWORD='re_resend'):
            self.assertEqual(resend_api_key(), 're_resend')
            self.assertEqual(brevo_api_key(), '')


class ResendBackendTests(SimpleTestCase):
    """Mail goes over HTTPS because the host blocks outbound SMTP.

    A connection to port 587 timed out rather than being refused, which is what
    a blocked port looks like from the inside. Nothing blocks 443.
    """

    def _backend(self, **kwargs):
        kwargs.setdefault('api_key', 're_test_key')
        return ResendBackend(**kwargs)

    def _message(self):
        return EmailMessage(
            subject='KTS password reset',
            body='Follow this link: https://kts.example/password-reset/a/b/',
            from_email='KTS <no-reply@kts.example>',
            to=['student@st.knust.edu.gh'],
        )

    def _sent_request(self, mocked):
        return mocked.call_args[0][0]

    def test_it_posts_the_message_the_way_resend_expects(self):
        with mock.patch('scheduler.mail.urllib.request.urlopen') as urlopen:
            self.assertEqual(self._backend().send_messages([self._message()]), 1)
        request = self._sent_request(urlopen)
        self.assertEqual(request.get_header('Authorization'), 'Bearer re_test_key')
        payload = json.loads(request.data.decode())
        self.assertEqual(payload['to'], ['student@st.knust.edu.gh'])
        self.assertEqual(payload['subject'], 'KTS password reset')
        self.assertIn('password-reset', payload['text'])

    def test_it_goes_over_https(self):
        """The whole reason this exists - 443 is the one port nothing blocks."""
        with mock.patch('scheduler.mail.urllib.request.urlopen') as urlopen:
            self._backend().send_messages([self._message()])
        self.assertTrue(self._sent_request(urlopen).full_url.startswith('https://'))

    def _failing_with(self, code, message):
        return urllib.error.HTTPError(
            'https://api.resend.com/emails', code, 'error', {},
            io.BytesIO(json.dumps({'message': message}).encode()))

    def test_what_resend_refuses_is_reported_as_something_to_act_on(self):
        cases = [
            (401, 'API key is invalid', 'has not been revoked'),
            (403, 'testing emails only', 'verify a domain'),
            (422, 'not a verified domain', 'domain you have verified'),
        ]
        for code, detail, advice in cases:
            with self.subTest(code=code):
                with mock.patch('scheduler.mail.urllib.request.urlopen',
                                side_effect=self._failing_with(code, detail)):
                    with self.assertRaises(MailDeliveryError) as caught:
                        self._backend().send_messages([self._message()])
                self.assertIn(advice, str(caught.exception))
                self.assertIn(detail, str(caught.exception))

    def test_a_missing_key_says_so_rather_than_failing_obscurely(self):
        with self.assertRaises(MailDeliveryError) as caught:
            self._backend(api_key='').send_messages([self._message()])
        self.assertIn('RESEND_API_KEY', str(caught.exception))

    def test_it_stays_quiet_when_asked_to(self):
        """A password reset sends with fail_silently, and must not become a
        server error because mail is misconfigured."""
        backend = self._backend(api_key='', fail_silently=True)
        self.assertEqual(backend.send_messages([self._message()]), 0)

    def test_the_key_is_found_where_resends_own_instructions_put_it(self):
        """Following the SMTP setup lands it in EMAIL_HOST_PASSWORD, and there
        is no reason to make someone move it to get mail working."""
        with override_settings(RESEND_API_KEY='', EMAIL_HOST_PASSWORD='re_from_smtp'):
            self.assertEqual(resend_api_key(), 're_from_smtp')

    def test_an_ordinary_smtp_password_is_not_mistaken_for_a_key(self):
        with override_settings(RESEND_API_KEY='', EMAIL_HOST_PASSWORD='hunter2'):
            self.assertEqual(resend_api_key(), '')

    def test_an_explicit_key_wins(self):
        with override_settings(RESEND_API_KEY='re_explicit',
                               EMAIL_HOST_PASSWORD='re_leftover'):
            self.assertEqual(resend_api_key(), 're_explicit')

    def test_it_cannot_hang_past_what_the_host_allows(self):
        with mock.patch('scheduler.mail.urllib.request.urlopen') as urlopen:
            self._backend().send_messages([self._message()])
        self.assertLessEqual(urlopen.call_args.kwargs['timeout'], 30)


class ErrorPageTests(SimpleTestCase):
    """What a server error looks like to whoever hits it."""

    def test_there_is_one(self):
        """The default is a bare line of text on white, which does not say
        whether they broke it or we did."""
        rendered = render_to_string('500.html')
        self.assertIn('KTS', rendered)
        self.assertIn('not something you did', rendered)

    def test_it_depends_on_nothing_that_could_be_the_fault(self):
        """A page that fetches a stylesheet cannot be shown when static files
        are what broke, and this is the page for when things are broken.

        Checked against the rendered output rather than the source, because
        that is what actually gets delivered - and because the source may
        perfectly well discuss a tag it does not use.
        """
        rendered = render_to_string('500.html')
        for fetches in ['<link', '<script', '<img', 'src=', '/static/', 'url(']:
            with self.subTest(fetches=fetches):
                self.assertNotIn(fetches, rendered)

    def test_it_renders_with_no_context_at_all(self):
        """Django's handler500 passes none: no request, no user, no context
        processors. A tag that needs any of them raises inside the error
        handler, and the visitor gets the bare default anyway."""
        self.assertIn('<html', render_to_string('500.html'))


class PasswordResetDeliveryTests(TestCase):
    """Django already treats a mail server that will not take the message as a
    non-event: PasswordResetForm.send_mail catches and logs it, so the reset
    still reports the same thing it reports for an address that is not on
    file. These hold that behaviour in place, because losing it would turn a
    misconfigured mail server into a 500 for whoever is resetting."""

    def setUp(self):
        self.student = Student.objects.create(
            student_id='20512099', name='Ama', email='ama@st.knust.edu.gh')
        create_student_account(self.student)
        self.student.refresh_from_db()

    def _reset_with_mail_failing_as(self, error):
        with mock.patch('django.core.mail.EmailMultiAlternatives.send',
                        side_effect=error):
            return self.client.post(
                '/password-reset/', {'email': self.student.user.email})

    def test_a_refused_connection_does_not_500(self):
        """A wrong EMAIL_HOST, or a port with nothing behind it."""
        response = self._reset_with_mail_failing_as(
            ConnectionRefusedError(61, 'Connection refused'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/password-reset/sent/')

    def test_rejected_credentials_do_not_500(self):
        """By far the likeliest: a provider that wants an app password."""
        response = self._reset_with_mail_failing_as(
            smtplib.SMTPAuthenticationError(
                535, b'5.7.8 Username and Password not accepted'))
        self.assertEqual(response.status_code, 302)

    def test_a_failed_send_looks_the_same_as_an_address_not_on_file(self):
        """The flow answers identically either way on purpose, so the form
        cannot be used to discover who has an account. An error page for real
        addresses only would hand that straight over."""
        broken = self._reset_with_mail_failing_as(ConnectionRefusedError())
        unknown = self.client.post(
            '/password-reset/', {'email': 'nobody@st.knust.edu.gh'})
        self.assertEqual(broken.status_code, unknown.status_code)
        self.assertEqual(broken.url, unknown.url)

    def test_a_working_send_still_sends(self):
        response = self.client.post(
            '/password-reset/', {'email': self.student.user.email})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/password-reset/', mail.outbox[0].body)


class TestEmailButtonTests(TestCase):
    """The reason a reset failed only reaches the log, and the host this runs
    on has no shell to read one with."""

    def setUp(self):
        self.admin = make_admin()
        self.admin.email = 'boss@knust.edu.gh'
        self.admin.save()
        self.client.force_login(self.admin)

    @override_settings(EMAIL_HOST='smtp.example.com')
    def test_it_reports_the_mail_servers_own_words(self):
        with mock.patch('scheduler.views.send_mail',
                        side_effect=smtplib.SMTPAuthenticationError(
                            535, b'5.7.8 Username and Password not accepted')):
            response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        body = response.content.decode()
        self.assertIn('SMTPAuthenticationError', body)
        self.assertIn('Username and Password not accepted', body)
        self.assertIn('smtp.example.com', body)

    @override_settings(EMAIL_HOST='smtp.example.com')
    def test_it_survives_a_failure_it_has_never_seen(self):
        """The button reported a blank server error page in production.

        It caught only OSError and SMTPException, but a mail configuration can
        fail while the backend is being constructed - before any connection is
        attempted - and that is neither. A diagnostic whose own failure mode is
        the blank page it exists to explain is worse than no diagnostic.
        """
        unheard_of = ValueError(
            'EMAIL_USE_TLS/EMAIL_USE_SSL are mutually exclusive')
        with mock.patch('scheduler.views.send_mail', side_effect=unheard_of):
            response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('mutually exclusive', response.content.decode())

    @override_settings(EMAIL_HOST='smtp.example.com')
    def test_known_faults_say_what_to_change(self):
        """A class name and a numeric code are not an instruction."""
        cases = [
            (smtplib.SMTPAuthenticationError(535, b'nope'), 'App Password'),
            (smtplib.SMTPSenderRefused(553, b'nope', 'a@b.c'), 'from address'),
            (TimeoutError('timed out'), 'STARTTLS belongs to 587'),
            (ConnectionRefusedError('refused'), 'Check the host name'),
        ]
        for error, advice in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch('scheduler.views.send_mail', side_effect=error):
                    response = self.client.post(
                        '/account/', {'test_email': '1'}, follow=True)
                self.assertIn(advice, response.content.decode())

    @override_settings(EMAIL_HOST='smtp.example.com', EMAIL_PORT=465,
                       EMAIL_USE_SSL=True, EMAIL_USE_TLS=False)
    def test_the_summary_names_the_settings_that_usually_disagree(self):
        with mock.patch('scheduler.views.send_mail',
                        side_effect=TimeoutError('timed out')):
            response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        body = response.content.decode()
        self.assertIn('port 465', body)
        self.assertIn('encryption SSL', body)

    @override_settings(EMAIL_HOST='smtp.example.com')
    def test_a_working_server_says_so(self):
        response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['boss@knust.edu.gh'])
        self.assertIn('Test email sent', response.content.decode())

    @override_settings(EMAIL_BACKEND='scheduler.mail.BrevoBackend',
                       MAIL_SENDER_NEEDS_SETTING=True)
    def test_an_unset_sender_is_called_out_before_it_wastes_anyones_time(self):
        """Everything reads as configured, and every message is refused."""
        body = self.client.get('/account/').content.decode()
        self.assertIn('DEFAULT_FROM_EMAIL', body)
        self.assertIn('No sender address has been set', body)

    @override_settings(EMAIL_BACKEND='scheduler.mail.ResendBackend',
                       DEFAULT_FROM_EMAIL='KTS <onboarding@resend.dev>',
                       MAIL_SENDER_NEEDS_SETTING=False)
    def test_resends_shared_sender_is_called_out_as_a_dead_end_for_students(self):
        """The trap: the administrator's own test arrives, so it all looks
        fine, and every student is quietly refused."""
        body = self.client.get('/account/').content.decode()
        # No apostrophe in the needle: the template escapes them, so the
        # literal sentence never appears in the rendered page.
        self.assertIn('reset will not', body)

    @override_settings(EMAIL_BACKEND='scheduler.mail.BrevoBackend',
                       DEFAULT_FROM_EMAIL='KTS <timetable@gmail.com>',
                       MAIL_SENDER_NEEDS_SETTING=False)
    def test_a_properly_set_up_provider_is_not_nagged_about(self):
        """Asserted on the words, not the class that styles them: every alert
        class is defined in the stylesheet, which is on every page, so looking
        for the class would pass whatever the page said."""
        body = self.client.get('/account/').content.decode()
        for nag in ['No sender address has been set',
                    'reset will not',
                    'No mail server is configured']:
            with self.subTest(nag=nag):
                self.assertNotIn(nag, body)

    @override_settings(EMAIL_HOST='')
    def test_an_unconfigured_server_is_called_out_rather_than_looking_fine(self):
        """With no host the backend writes to the log and reports success, so
        a plain send would claim everything works."""
        response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        self.assertIn('No mail server is configured', response.content.decode())

    def test_it_needs_an_address_to_send_to(self):
        self.admin.email = ''
        self.admin.save()
        response = self.client.post('/account/', {'test_email': '1'}, follow=True)
        self.assertIn('Add your own email address first', response.content.decode())
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST='smtp.example.com')
    def test_it_is_not_offered_to_anyone_else(self):
        """A student pressing it would send mail on the deployment's account."""
        student = Student.objects.create(student_id='20512001', name='Ama')
        create_student_account(student)
        student.refresh_from_db()
        self.client.force_login(student.user)

        self.assertNotIn('Email delivery',
                         self.client.get('/account/').content.decode())
        self.client.post('/account/', {'test_email': '1'}, follow=True)
        self.assertEqual(len(mail.outbox), 0)


class TimetableDownloadTests(TestCase):
    """Taking the timetable away with you."""

    def setUp(self):
        build_dataset()
        run_genetic_algorithm()

    def _rows(self, response):
        body = response.content.decode()
        return list(csv.reader(io.StringIO(body)))

    def test_an_admin_gets_the_whole_week(self):
        self.client.force_login(make_admin())
        response = self.client.get('/timetable/download/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn('attachment;', response['Content-Disposition'])

        rows = self._rows(response)
        self.assertEqual(rows[0], ['Day', 'Start', 'End', 'Course code',
                                   'Course name', 'Lecturer', 'Room',
                                   'Student group'])
        self.assertEqual(len(rows) - 1, TimetableEntry.objects.filter(
            is_active=True).count())

    def test_a_student_gets_their_own_group_and_no_one_elses(self):
        group = StudentGroup.objects.first()
        student = Student.objects.create(
            student_id='20512001', name='Ama', group=group)
        create_student_account(student)
        student.refresh_from_db()
        self.client.force_login(student.user)

        rows = self._rows(self.client.get('/timetable/download/'))[1:]
        self.assertTrue(rows, 'the student has classes but got an empty file')
        self.assertEqual({row[7] for row in rows}, {group.name})

    def test_a_student_cannot_widen_it_with_a_query_string(self):
        """The page gives them no filter controls, so the file must not accept
        one either - the URL is the obvious thing to try."""
        mine, theirs = StudentGroup.objects.all()[:2]
        student = Student.objects.create(
            student_id='20512001', name='Ama', group=mine)
        create_student_account(student)
        student.refresh_from_db()
        self.client.force_login(student.user)

        rows = self._rows(self.client.get(
            f'/timetable/download/?group={theirs.pk}'))[1:]
        self.assertEqual({row[7] for row in rows}, {mine.name})

    def test_the_file_is_named_for_the_student(self):
        student = Student.objects.create(
            student_id='20512001', name='Ama', group=StudentGroup.objects.first())
        create_student_account(student)
        student.refresh_from_db()
        self.client.force_login(student.user)
        response = self.client.get('/timetable/download/')
        self.assertIn('20512001', response['Content-Disposition'])

    def test_a_filter_on_the_page_is_a_filter_on_the_file(self):
        """Otherwise Download quietly hands over the whole department after you
        narrowed the page to one group."""
        self.client.force_login(make_admin())
        group = StudentGroup.objects.first()
        rows = self._rows(self.client.get(
            f'/timetable/download/?group={group.pk}'))[1:]
        self.assertEqual({row[7] for row in rows}, {group.name})

    def test_it_reads_monday_first(self):
        """Ordering on the raw day code files it FRI, MON, THU, TUE, WED, which
        is nobody's week."""
        self.client.force_login(make_admin())
        days = [row[0] for row in self._rows(
            self.client.get('/timetable/download/'))[1:]]
        week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        self.assertEqual(days, sorted(days, key=week.index))

    def test_it_needs_a_login(self):
        response = self.client.get('/timetable/download/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_the_page_offers_it(self):
        self.client.force_login(make_admin())
        self.assertIn('/timetable/download/',
                      self.client.get('/timetable/').content.decode())


class MobileTableTests(TestCase):
    """On a phone the tables stop being tables.

    The header row is dropped and every cell carries its own label, so the two
    have to agree - and they are written in different places, which is exactly
    the arrangement that drifts when a column is renamed.
    """

    LIST_PAGES = ['/lecturers/', '/courses/', '/rooms/', '/student-groups/',
                  '/students/', '/timeslots/']

    def setUp(self):
        build_dataset()
        Student.objects.create(student_id='20512001', name='Ama')
        self.client.force_login(make_admin())

    def _table(self, url):
        body = self.client.get(url).content.decode()
        return re.search(r'<table class="data-table">.*?</table>', body, re.S).group(0)

    def test_every_cell_is_labelled_with_its_own_column(self):
        for url in self.LIST_PAGES:
            with self.subTest(url=url):
                table = self._table(url)
                headings = [re.sub(r'<[^>]+>', '', h).strip()
                            for h in re.findall(r'<th[^>]*>(.*?)</th>', table, re.S)]
                first_row = re.search(r'<tbody>.*?<tr>(.*?)</tr>', table, re.S).group(1)
                labels = re.findall(r'<td[^>]*?data-label="([^"]*)"', first_row)

                # Every column but the last, which is the row's buttons and
                # needs no label beside two icons.
                self.assertEqual(labels, headings[:-1])

    def test_the_actions_column_is_left_unlabelled(self):
        """"Actions" next to a pencil and a bin is noise."""
        for url in self.LIST_PAGES:
            with self.subTest(url=url):
                self.assertNotIn('data-label="Actions"', self._table(url))

    def test_the_timetable_ships_both_shapes(self):
        run_genetic_algorithm()
        body = self.client.get('/timetable/').content.decode()
        self.assertIn('timetable-grid', body)
        self.assertIn('timetable-agenda', body)

    def test_the_agenda_says_when_a_day_is_free(self):
        """A day with nothing in it is worth knowing about; a blank card is
        not the same as being told."""
        TimetableEntry.objects.all().delete()
        body = self.client.get('/timetable/').content.decode()
        self.assertIn('No classes.', body)


class MobileLayoutTests(TestCase):
    """The shell has to survive a phone.

    The sidebar is fixed at 250px with an equal margin holding the content
    clear of it, which on a 390px screen leaves the page about 140px to live
    in. Below the tablet breakpoint the sidebar becomes a drawer instead.
    These guard the pieces of that which are easy to break from a distance.
    """

    TEMPLATES = Path(__file__).resolve().parent / 'templates' / 'scheduler'

    def _template(self, name):
        return (self.TEMPLATES / name).read_text(encoding='utf-8')

    def _drawer_media_block(self):
        """Find the @media block that folds the sidebar away, and the width it
        does it at.

        Located by what it does - it is the one that turns the drawer button
        on - rather than by a width written down here as well, so the
        breakpoint has exactly one home.
        """
        css = self._template('base.html')
        for match in re.finditer(r'@media \(max-width: (\d+(?:\.\d+)?)px\)', css):
            depth, opened = 0, css.index('{', match.end() - 1)
            for end in range(opened, len(css)):
                if css[end] == '{':
                    depth += 1
                elif css[end] == '}':
                    depth -= 1
                    if depth == 0:
                        break
            else:
                self.fail('a media block is never closed')
            body = css[opened:end]
            if '.nav-toggle' in body:
                return float(match.group(1)), body
        self.fail('no media block turns the drawer button on')

    def test_both_shells_declare_a_viewport(self):
        """Without this a phone lays the page out at 980px and scales the
        result down, so every media query below resolves against the wrong
        width and the text arrives too small to read."""
        for shell in ['base.html', 'auth_base.html']:
            with self.subTest(shell=shell):
                self.assertIn('width=device-width', self._template(shell))

    def test_the_app_shell_ships_the_drawer(self):
        self.client.force_login(make_admin())
        body = self.client.get('/').content.decode()
        for element in ['id="sidebar"', 'id="nav-toggle"',
                        'id="nav-backdrop"', 'id="nav-close"']:
            with self.subTest(element=element):
                self.assertIn(element, body)
        self.assertIn('nav-drawer.js', body)

    def test_the_drawer_script_is_served(self):
        """Asked of the finders rather than of a URL: serving it also depends
        on collectstatic having been run, which is true of a deploy but not of
        a fresh clone, and that is not what this is checking."""
        self.assertIsNotNone(finders.find('scheduler/nav-drawer.js'))

    def test_the_content_is_not_held_clear_of_a_sidebar_that_has_gone(self):
        """The margin is what reserves the sidebar's 250px. Leave it in place
        once the sidebar is off-canvas and the page is 250px narrower than the
        phone for no reason at all - which is the whole bug."""
        _, block = self._drawer_media_block()
        self.assertRegex(block, r'\.main-wrap\s*\{[^}]*margin-left:\s*0')

    def test_the_stylesheet_and_the_script_agree_on_the_breakpoint(self):
        """The CSS decides when the sidebar folds away; the script decides when
        to drop the open state. If the two drift apart there is a band of
        widths where the drawer is the only navigation and cannot be opened,
        or where it stays latched open over a sidebar that is already visible.
        """
        script = (Path(__file__).resolve().parent / 'static' / 'scheduler'
                  / 'nav-drawer.js').read_text(encoding='utf-8')
        css_max, _ = self._drawer_media_block()
        js_min = float(re.search(r'min-width:\s*(\d+)px', script).group(1))
        self.assertLess(css_max, js_min)
        self.assertLess(js_min - css_max, 1.0,
                        f'the drawer folds away at {css_max}px and the script '
                        f'gives up on it at {js_min}px; widths between the two '
                        f'are covered by neither')

    def test_list_pages_use_the_page_header_component(self):
        """A row of utility classes cannot be taught to wrap. The named
        component can, and does, in the media block above."""
        pages = ['lecturers.html', 'courses.html', 'rooms.html',
                 'studentgroups.html', 'students.html', 'timeslots.html',
                 'notifications.html']
        for page in pages:
            with self.subTest(page=page):
                markup = self._template(page)
                self.assertIn('class="page-header mb-4"', markup)
                self.assertIn('class="page-header-actions"', markup)
                self.assertNotIn('d-flex justify-content-between', markup)

    def test_the_drawer_opens_over_the_page_rather_than_beside_it(self):
        """A drawer as wide as the screen reads as a new page, and there is
        then nothing left showing to tap to get back."""
        _, block = self._drawer_media_block()
        self.assertRegex(block, r'max-width:\s*\d+vw')


class StudentEmailTests(TestCase):
    def test_an_account_takes_the_students_address(self):
        student = Student.objects.create(
            student_id='20512001', name='Ama', email='ama@st.knust.edu.gh')
        user, _password = create_student_account(student)
        self.assertEqual(user.email, 'ama@st.knust.edu.gh')

    def test_a_student_can_reset_after_their_account_is_made(self):
        student = Student.objects.create(
            student_id='20512001', name='Ama', email='ama@st.knust.edu.gh')
        create_student_account(student)
        self.client.post('/password-reset/', {'email': 'ama@st.knust.edu.gh'})
        self.assertEqual(len(mail.outbox), 1)

    def test_an_account_without_an_address_is_still_created(self):
        student = Student.objects.create(student_id='20512002', name='Kojo')
        user, _password = create_student_account(student)
        self.assertEqual(user.email, '')

    def test_the_importer_reads_an_email_column(self):
        result = run_import('students', csv_upload(
            'student_id,name,email\n20512001,Ama,ama@st.knust.edu.gh\n'))
        self.assertEqual(result.created, 1)
        self.assertEqual(Student.objects.get().email, 'ama@st.knust.edu.gh')

    def test_a_students_file_with_emails_is_not_taken_for_lecturers(self):
        with self.assertRaises(CsvImportError):
            run_import('lecturers', csv_upload(template_csv('students')))


class SearchTests(TestCase):
    def setUp(self):
        self.client.force_login(make_admin('searcher'))
        self.mensah = Lecturer.objects.create(
            name='Dr. Kwame Mensah', email='kmensah@knust.edu.gh')
        self.boateng = Lecturer.objects.create(
            name='Prof. Ama Boateng', email='aboateng@knust.edu.gh')
        self.course = Course.objects.create(
            code='CS 451', name='Distributed Systems',
            expected_students=90, lecturer=self.mensah)
        Course.objects.create(code='CS 453', name='Machine Learning',
                              expected_students=90, lecturer=self.boateng)
        Room.objects.create(name='PB 001 Lecture Hall', capacity=250)
        Room.objects.create(name='CS Lab 1', capacity=60)
        self.group = StudentGroup.objects.create(name='CS Level 400 Group 1')
        self.group.courses.set([self.course])
        StudentGroup.objects.create(name='CS Level 100 Group 1')
        Student.objects.create(
            student_id='20212007', index_number='7212007', name='Adjoa Mensimah',
            programme='BSc Computer Science', level='400', group=self.group)
        Student.objects.create(
            student_id='20512001', index_number='7212001', name='Ama Serwaa',
            programme='BSc Computer Science', level='100')

    def _names(self, url, key):
        return [str(o) for o in self.client.get(url).context[key]]

    def test_each_page_searches_its_own_records(self):
        cases = [
            ('/lecturers/?q=mensah', 'lecturers', 1),
            ('/courses/?q=machine', 'courses', 1),
            ('/rooms/?q=lab', 'rooms', 1),
            ('/student-groups/?q=level 400', 'groups', 1),
            ('/students/?q=adjoa', 'students', 1),
        ]
        for url, key, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(len(self._names(url, key)), expected)

    def test_search_is_case_insensitive(self):
        for term in ('mensah', 'MENSAH', 'MeNsAh'):
            with self.subTest(term=term):
                self.assertEqual(len(self._names(f'/lecturers/?q={term}', 'lecturers')), 1)

    def test_search_matches_a_fragment(self):
        self.assertEqual(len(self._names('/lecturers/?q=ensa', 'lecturers')), 1)

    def test_lecturers_are_searchable_by_email(self):
        self.assertEqual(
            len(self._names('/lecturers/?q=aboateng', 'lecturers')), 1)

    def test_courses_are_searchable_by_their_lecturer(self):
        found = self._names('/courses/?q=Boateng', 'courses')
        self.assertEqual(len(found), 1)
        self.assertIn('CS 453', found[0])

    def test_students_are_searchable_by_index_number(self):
        found = self._names('/students/?q=7212007', 'students')
        self.assertEqual(len(found), 1)
        self.assertIn('Adjoa', found[0])

    def test_students_are_searchable_by_group(self):
        found = self._names('/students/?q=Group 1', 'students')
        self.assertEqual(len(found), 1)
        self.assertIn('Adjoa', found[0])

    def test_groups_are_searchable_by_the_courses_they_take(self):
        found = self._names('/student-groups/?q=CS 451', 'groups')
        self.assertEqual(len(found), 1)

    def test_an_empty_search_shows_everything(self):
        self.assertEqual(len(self._names('/lecturers/?q=', 'lecturers')), 2)
        self.assertEqual(len(self._names('/lecturers/', 'lecturers')), 2)

    def test_whitespace_only_is_treated_as_empty(self):
        response = self.client.get('/lecturers/?q=%20%20')
        self.assertEqual(len(response.context['lecturers']), 2)
        self.assertIsNone(response.context['result_count'])

    def test_no_matches_says_so_and_offers_a_way_back(self):
        response = self.client.get('/lecturers/?q=nobodyhere')
        self.assertEqual(len(response.context['lecturers']), 0)
        self.assertContains(response, 'Nothing matches')
        self.assertContains(response, 'Show all 2')

    def test_the_total_stays_the_full_count(self):
        """The header still reports the collection, not the current filter."""
        response = self.client.get('/lecturers/?q=mensah')
        self.assertEqual(response.context['total_count'], 2)
        self.assertEqual(response.context['result_count'], 1)

    def test_the_term_is_kept_in_the_box(self):
        response = self.client.get('/lecturers/?q=mensah')
        self.assertContains(response, 'value="mensah"')

    def test_a_group_matching_two_courses_appears_once(self):
        """A join across a many-to-many would otherwise duplicate the row."""
        second = Course.objects.create(code='CS 455', name='Computer Graphics',
                                       expected_students=70)
        self.group.courses.add(second)
        found = self._names('/student-groups/?q=CS 4', 'groups')
        self.assertEqual(len(found), 1)

    def test_paging_keeps_the_search(self):
        """Without the term in the page links, page two silently shows
        everything."""
        for i in range(60):
            Lecturer.objects.create(name=f'Dr Findme {i}', email=f'f{i}@x.gh')
        response = self.client.get('/lecturers/?q=findme')
        self.assertEqual(response.context['lecturers'].paginator.count, 60)
        self.assertIn('q=findme&amp;page=2', response.content.decode())

        page_two = self.client.get('/lecturers/?q=findme&page=2')
        self.assertEqual(page_two.context['lecturers'].paginator.count, 60)
        for lecturer in page_two.context['lecturers']:
            self.assertIn('Findme', lecturer.name)

    def test_a_term_with_spaces_survives_the_page_link(self):
        for i in range(60):
            Lecturer.objects.create(name=f'Dr Ama Serwaa {i}', email=f'a{i}@x.gh')
        response = self.client.get('/lecturers/?q=ama serwaa')
        self.assertIn('q=ama%20serwaa&amp;page=2', response.content.decode())

    def test_search_is_available_to_admins_only(self):
        build_dataset()
        self.client.force_login(make_lecturer_user('outsider'))
        self.assertIn(
            self.client.get('/lecturers/?q=mensah').status_code, (302, 403))


class BulkDeleteTests(TestCase):
    KINDS = {
        'lecturers': Lecturer,
        'courses': Course,
        'rooms': Room,
        'student-groups': StudentGroup,
        'students': Student,
        'timeslots': TimeSlot,
    }

    def setUp(self):
        build_dataset()
        Student.objects.create(
            student_id='20500001', name='Ama', group=StudentGroup.objects.first()
        )
        self.admin = make_admin('bulkadmin')
        self.client.force_login(self.admin)

    def test_get_only_confirms_and_deletes_nothing(self):
        for kind, model in self.KINDS.items():
            with self.subTest(kind=kind):
                before = model.objects.count()
                response = self.client.get(f'/{kind}/delete-all/')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(model.objects.count(), before)

    def test_post_empties_the_table(self):
        for kind, model in self.KINDS.items():
            with self.subTest(kind=kind):
                self.client.post(f'/{kind}/delete-all/')
                self.assertEqual(model.objects.count(), 0)

    def test_the_confirmation_states_the_count(self):
        response = self.client.get('/rooms/delete-all/')
        self.assertContains(response, f'Delete all {Room.objects.count()}')

    def test_the_confirmation_warns_about_the_timetable(self):
        run_genetic_algorithm()
        entries = TimetableEntry.objects.count()
        self.assertGreater(entries, 0)
        response = self.client.get('/rooms/delete-all/')
        self.assertContains(response, 'the whole timetable')
        self.assertContains(response, str(entries))

    def test_deleting_rooms_takes_the_timetable_with_it(self):
        run_genetic_algorithm()
        self.assertGreater(TimetableEntry.objects.count(), 0)
        self.client.post('/rooms/delete-all/')
        self.assertEqual(TimetableEntry.objects.count(), 0)

    def test_deleting_lecturers_leaves_the_courses(self):
        """SET_NULL, so courses survive without a lecturer."""
        before = Course.objects.count()
        self.client.post('/lecturers/delete-all/')
        self.assertEqual(Course.objects.count(), before)
        self.assertEqual(Course.objects.filter(lecturer__isnull=False).count(), 0)

    def test_deleting_groups_leaves_the_students(self):
        self.client.post('/student-groups/delete-all/')
        student = Student.objects.get(student_id='20500001')
        self.assertIsNone(student.group)

    def test_the_confirmation_warns_about_orphaned_accounts(self):
        student = Student.objects.get(student_id='20500001')
        create_student_account(student)
        response = self.client.get('/students/delete-all/')
        self.assertContains(response, 'login account')

    def test_an_empty_table_says_so_rather_than_offering_the_button(self):
        Room.objects.all().delete()
        response = self.client.get('/rooms/delete-all/')
        self.assertContains(response, 'no rooms to delete')
        self.assertNotContains(response, 'Yes, delete all')

    def test_an_unknown_kind_404s(self):
        self.assertEqual(self.client.get('/nonsense/delete-all/').status_code, 404)

    def test_a_lecturer_cannot_delete_everything(self):
        self.client.force_login(make_lecturer_user('nothanks'))
        before = Room.objects.count()
        self.assertIn(self.client.get('/rooms/delete-all/').status_code, (302, 403))
        self.assertIn(self.client.post('/rooms/delete-all/').status_code, (302, 403))
        self.assertEqual(Room.objects.count(), before)

    def test_a_student_cannot_delete_everything(self):
        student = make_student(student_id='20500999')
        self.client.force_login(student.user)
        before = Course.objects.count()
        self.assertIn(self.client.post('/courses/delete-all/').status_code, (302, 403))
        self.assertEqual(Course.objects.count(), before)

    def test_every_list_page_offers_the_button(self):
        pages = {
            '/lecturers/': 'lecturers', '/courses/': 'courses', '/rooms/': 'rooms',
            '/student-groups/': 'student-groups', '/students/': 'students',
            '/timeslots/': 'timeslots',
        }
        for url, kind in pages.items():
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn(f'/{kind}/delete-all/', body)

    def test_the_import_page_does_not_offer_it(self):
        # Asserted on the URL, not the class name: .btn-delete-all lives in the
        # stylesheet that every page carries, and would match either way.
        body = self.client.get('/import/').content.decode()
        self.assertNotIn('/delete-all/', body)


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
        """The check must still fire when the group genuinely does not fit.

        The placement is made directly rather than by running the algorithm.
        What is being tested is the capacity check, and reaching it through the
        search made the test depend on the search's luck: entries that collide
        on a room and time are dropped before saving, and on roughly one seed
        in ten the oversized class was the one dropped, leaving nothing to flag
        and the test failing for a reason that had nothing to do with capacity.
        """
        huge = StudentGroup.objects.create(name='Huge', size=500)
        huge.courses.set([self.course])
        room = Room.objects.first()
        self.assertLess(room.capacity, huge.size)

        TimetableEntry.objects.create(
            course=self.course,
            room=room,
            timeslot=TimeSlot.objects.first(),
            student_group=huge,
            is_active=True,
        )

        types = {c['type'] for c in detect_conflicts()}
        self.assertIn('Room Capacity Mismatch', types)


class StudentFieldTests(TestCase):
    """Student ID, index number, programme and level are four separate things."""

    def setUp(self):
        self.group = StudentGroup.objects.create(name='CS Level 400')
        self.client.force_login(make_admin('fieldadmin'))

    def test_all_four_are_stored_separately(self):
        """Programme is no longer typed: a department is what sets it."""
        department = Department.objects.get(name='Computer Science')
        self.client.post('/students/add/', {
            'student_id': '20212007',
            'index_number': '7212007',
            'name': 'Adjoa Mensimah',
            'email': 'adjoa@st.knust.edu.gh',
            'college': department.college.pk,
            'department': department.pk,
            'level': '400',
            'group': self.group.pk,
        })
        student = Student.objects.get(student_id='20212007')
        self.assertEqual(student.index_number, '7212007')
        self.assertEqual(student.department, department)
        self.assertEqual(student.programme, 'Computer Science')
        self.assertEqual(student.level, '400')
        self.assertEqual(student.group, self.group)

    def test_a_record_may_still_hold_only_a_student_id(self):
        """The form insists on more, because somebody typing one record in has
        all of it to hand. The model does not, because an import has to take
        the roster as it comes - so this goes at the model rather than through
        the form that used to allow it.
        """
        student = Student.objects.create(student_id='20212008', name='Nana Yaw',
                                         index_number='')
        student.refresh_from_db()
        self.assertIsNone(student.index_number)
        self.assertEqual(student.programme, '')
        self.assertEqual(student.email, '')

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
        """A programme this system knows as a department becomes it.

        The roster writes "BSc Computer Science"; the department is called
        Computer Science; they are the same thing, so the import settles on
        the department's own name rather than leaving two spellings of one
        answer in the database.
        """
        result = run_import('students', csv_upload(
            'student_id,index_number,name,programme,level,group\n'
            '20212007,7212007,Adjoa,BSc Computer Science,400,CS Level 400\n'))
        self.assertEqual(result.created, 1)
        student = Student.objects.get()
        self.assertEqual(student.index_number, '7212007')
        self.assertEqual(student.department.name, 'Computer Science')
        self.assertEqual(student.college.name, 'College of Science')
        self.assertEqual(student.programme, 'Computer Science')
        self.assertEqual(student.level, '400')
        self.assertEqual(student.group.name, 'CS Level 400')

    def test_a_programme_with_no_department_is_kept_as_written(self):
        """The roster is still right; this system is only missing a department
        for it. Rewriting or dropping it would lose what was imported."""
        result = run_import('students', csv_upload(
            'student_id,name,programme\n20212008,Kofi,BA Akan Studies\n'))
        self.assertEqual(result.created, 1)
        student = Student.objects.get(student_id='20212008')
        self.assertIsNone(student.department)
        self.assertEqual(student.programme, 'BA Akan Studies')

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


class RepeatedIdentifierTests(TestCase):
    """Rows sharing an identifier overwrite each other, so a big file can
    produce a small table. That has to be said out loud."""

    def test_repeats_are_counted(self):
        rows = '\n'.join(f'205{i % 3:05d},Student {i}' for i in range(30))
        result = run_import('students', csv_upload(f'student_id,name\n{rows}\n'))
        self.assertEqual(result.rows_read, 30)
        self.assertEqual(result.repeated, 27)
        self.assertEqual(result.records, 3)
        self.assertEqual(Student.objects.count(), 3)

    def test_records_matches_what_lands_in_the_table(self):
        rows = '\n'.join(f'205{i % 28:05d},Student {i}' for i in range(651))
        result = run_import('students', csv_upload(f'student_id,name\n{rows}\n'))
        self.assertEqual(result.records, Student.objects.count())
        self.assertEqual(result.records, 28)

    def test_a_clean_file_reports_no_repeats(self):
        rows = '\n'.join(f'205{i:05d},Student {i}' for i in range(20))
        result = run_import('students', csv_upload(f'student_id,name\n{rows}\n'))
        self.assertEqual(result.repeated, 0)
        self.assertEqual(result.records, 20)

    def test_the_identifying_column_is_reported_by_its_own_header(self):
        """So the operator can tell which column was treated as the identifier."""
        result = run_import('students', csv_upload(
            'Student Number,Full Name\n20500001,Ama\n'))
        self.assertEqual(result.identifier_column, 'Student Number')

    def test_examples_of_the_repeated_values_are_given(self):
        result = run_import('students', csv_upload(
            'student_id,name\n20500001,A\n20500001,B\n20500002,C\n20500002,D\n'))
        self.assertEqual(sorted(result.repeated_examples), ['20500001', '20500002'])

    def test_the_warning_reaches_the_page(self):
        self.client.force_login(make_admin('dupeadmin'))
        rows = '\n'.join(f'205{i % 4:05d},Student {i}' for i in range(20))
        response = self.client.post('/import/', {
            'kind': 'students',
            'file': csv_upload(f'student_id,name\n{rows}\n'),
        })
        body = response.content.decode()
        self.assertIn('overwrote each other', body)
        self.assertIn('20 rows became 4 record(s)', body)

    def test_repeats_apply_to_every_kind(self):
        cases = {
            'lecturers': 'name,email\nA,same@x.gh\nB,same@x.gh\n',
            'rooms': 'name,capacity\nPB 001,50\nPB 001,60\n',
            'courses': 'code,name\nCS 151,A\nCS 151,B\n',
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                result = run_import(kind, csv_upload(text))
                self.assertEqual(result.repeated, 1)
                self.assertEqual(result.records, 1)


class TimeSlotImportTests(TestCase):
    def test_the_basic_file_imports(self):
        result = run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Monday,10:00,12:00\n'
            'Tuesday,08:00,10:00\n'))
        self.assertEqual(result.created, 3)
        self.assertEqual(TimeSlot.objects.count(), 3)

    def test_days_may_be_written_any_of_the_usual_ways(self):
        for text, expected in [('Monday', 'MON'), ('MON', 'MON'), ('mon', 'MON'),
                               ('Mon.', 'MON'), ('THURSDAY', 'THU'), ('thu', 'THU')]:
            with self.subTest(day=text):
                TimeSlot.objects.all().delete()
                run_import('timeslots', csv_upload(
                    f'day,start_time,end_time\n{text},08:00,10:00\n'))
                self.assertEqual(TimeSlot.objects.get().day, expected)

    def test_times_may_be_written_any_of_the_usual_ways(self):
        for text, hour in [('08:00', 8), ('8:00', 8), ('8:00 AM', 8),
                           ('1:00 PM', 13), ('13:00', 13), ('13:00:00', 13)]:
            with self.subTest(time=text):
                TimeSlot.objects.all().delete()
                run_import('timeslots', csv_upload(
                    f'day,start_time,end_time\nMonday,{text},23:00\n'))
                self.assertEqual(TimeSlot.objects.get().start_time.hour, hour)

    def test_alternative_headers_are_recognised(self):
        result = run_import('timeslots', csv_upload(
            'Weekday,From,To\nMonday,08:00,10:00\n'))
        self.assertEqual(result.created, 1)

    def test_reimporting_the_same_file_adds_nothing(self):
        text = ('day,start_time,end_time\n'
                'Monday,08:00,10:00\nTuesday,08:00,10:00\n')
        run_import('timeslots', csv_upload(text))
        result = run_import('timeslots', csv_upload(text))
        self.assertEqual(TimeSlot.objects.count(), 2)
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 2)

    def test_a_slot_differs_by_any_of_its_three_fields(self):
        run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Monday,08:00,09:00\n'
            'Tuesday,08:00,10:00\n'))
        self.assertEqual(TimeSlot.objects.count(), 3)

    def test_an_unreadable_day_is_skipped_with_a_reason(self):
        result = run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Sometime,08:00,10:00\n'))
        self.assertEqual(result.created, 1)
        self.assertIn('not a weekday', result.skipped[0])

    def test_an_unreadable_time_is_skipped_with_a_reason(self):
        result = run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Tuesday,morning,10:00\n'))
        self.assertEqual(result.created, 1)
        self.assertIn('cannot read the start time', result.skipped[0])

    def test_a_slot_ending_before_it_starts_is_skipped(self):
        result = run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Monday,10:00,08:00\n'))
        self.assertEqual(TimeSlot.objects.count(), 1)
        self.assertIn('ends before it starts', result.skipped[0])

    def test_a_file_of_nothing_but_bad_slots_is_refused_whole(self):
        """Right headers, nothing usable in them - one sentence beats a list."""
        with self.assertRaises(CsvImportError) as ctx:
            run_import('timeslots', csv_upload(
                'day,start_time,end_time\nMonday,10:00,08:00\n'))
        self.assertIn('None of the 1 rows', str(ctx.exception))
        self.assertEqual(TimeSlot.objects.count(), 0)

    def test_repeats_within_the_file_are_counted_on_the_whole_slot(self):
        result = run_import('timeslots', csv_upload(
            'day,start_time,end_time\n'
            'Monday,08:00,10:00\n'
            'Monday,08:00,10:00\n'
            'Monday,10:00,12:00\n'))
        self.assertEqual(result.rows_read, 3)
        self.assertEqual(result.repeated, 1)
        self.assertEqual(TimeSlot.objects.count(), 2)

    def test_a_timeslots_file_is_not_taken_for_another_kind(self):
        for other in ('lecturers', 'rooms', 'courses', 'students'):
            with self.subTest(into=other):
                with self.assertRaises(CsvImportError):
                    run_import(other, csv_upload(template_csv('timeslots')))

    def test_another_kinds_file_is_not_taken_for_timeslots(self):
        for other in ('lecturers', 'rooms', 'courses', 'students'):
            with self.subTest(uploading=other):
                with self.assertRaises(CsvImportError):
                    run_import('timeslots', csv_upload(template_csv(other)))

    def test_a_large_import_stays_bounded(self):
        rows = []
        for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'):
            for hour in range(7, 20):
                rows.append(f'{day},{hour:02d}:00,{hour + 1:02d}:00')
        with CaptureQueriesContext(connection) as ctx:
            result = run_import(
                'timeslots', csv_upload('day,start_time,end_time\n' + '\n'.join(rows) + '\n'))
        self.assertEqual(result.created, 65)
        self.assertLess(len(ctx), 20)

    def test_the_page_offers_time_slots(self):
        self.client.force_login(make_admin('slotadmin'))
        response = self.client.get('/import/?kind=timeslots')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Time Slots')


class ImportScaleTests(TestCase):
    """A per-row import is fine on a local file-backed database and fails on a
    networked one, where every query is a round trip. These pin the shape."""

    def test_a_large_student_import_is_a_handful_of_queries(self):
        # A bound, not an exact count: writes are batched, so the number moves
        # in steps with the file size. What must hold is that it is a handful
        # rather than a multiple of the row count.
        rows = '\n'.join(
            f'205{i:05d},72{i:05d},Student {i},BSc Computer Science,400,CS Level 400'
            for i in range(400)
        )
        upload = csv_upload(
            'student_id,index_number,name,programme,level,group\n' + rows + '\n')
        with CaptureQueriesContext(connection) as ctx:
            result = run_import('students', upload)
        self.assertEqual(result.created, 400)
        self.assertLess(len(ctx), 20, f'{len(ctx)} queries for 400 rows')

    def test_query_count_does_not_grow_with_the_file(self):
        def queries_for(count):
            Student.objects.all().delete()
            StudentGroup.objects.all().delete()
            rows = '\n'.join(f'205{i:05d},Student {i}' for i in range(count))
            with CaptureQueriesContext(connection) as ctx:
                run_import('students', csv_upload(f'student_id,name\n{rows}\n'))
            return len(ctx)

        small, large = queries_for(10), queries_for(500)
        self.assertEqual(Student.objects.count(), 500)
        self.assertEqual(
            small, large,
            f'{small} queries for 10 rows but {large} for 500 - it scales per row',
        )

    def test_every_kind_imports_in_a_bounded_number_of_queries(self):
        files = {
            'lecturers': 'name,email\n' + '\n'.join(
                f'L{i},l{i}@x.gh' for i in range(200)) + '\n',
            'rooms': 'name,capacity\n' + '\n'.join(
                f'Room {i},50' for i in range(200)) + '\n',
            'courses': 'code,name,expected_students\n' + '\n'.join(
                f'C{i},Course {i},40' for i in range(200)) + '\n',
        }
        for kind, text in files.items():
            with self.subTest(kind=kind):
                with CaptureQueriesContext(connection) as ctx:
                    run_import(kind, csv_upload(text))
                self.assertLess(
                    len(ctx), 20,
                    f'{kind} took {len(ctx)} queries for 200 rows',
                )

    def test_a_reimport_of_the_same_file_is_also_bounded(self):
        """The update path must not fall back to one query per row either."""
        text = 'name,email\n' + '\n'.join(f'L{i},l{i}@x.gh' for i in range(200)) + '\n'
        run_import('lecturers', csv_upload(text))
        with CaptureQueriesContext(connection) as ctx:
            result = run_import('lecturers', csv_upload(text))
        self.assertEqual(result.updated, 200)
        self.assertLess(len(ctx), 20)


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
