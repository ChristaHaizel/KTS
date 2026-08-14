import logging

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .permissions import (
    admin_required, is_admin, lecturer_for, staff_required, student_for,
)
from .models import (
    TimetableEntry, RescheduleRequest, Room, Lecturer, Course, StudentGroup,
    TimeSlot, GenerationRun, Student, Notification,
)
from .accounts import create_student_account, derive_username, generate_password
from .baselines import compare
from .charts import convergence_chart
from .conflict_detector import detect_conflicts
from .middleware import SESSION_KEY as IMPERSONATE_SESSION_KEY
from .importers import KINDS, ImportError_ as CsvImportError, run_import, template_csv
from .genetic_algorithm import (
    run_genetic_algorithm, PENALTY_ROOM_CLASH, PENALTY_LECTURER_CLASH,
    PENALTY_GROUP_CLASH, PENALTY_OVER_CAPACITY,
)
from .forms import (
    MAIL_DELIVERY_ERRORS, LecturerForm, CourseForm, MyEmailForm, RoomForm,
    StudentForm, StudentGroupForm, TimeSlotForm,
)
from .notifications import notify, notify_all_students, notify_group_of_change

logger = logging.getLogger(__name__)

PAGE_SIZE = 25


def _paginate(request, queryset):
    """Fine at fifteen courses, necessary at five hundred."""
    return Paginator(queryset, PAGE_SIZE).get_page(request.GET.get('page'))


def _list_context(request, queryset, fields, name):
    """Everything a searchable, paginated list page needs.

    The search runs before pagination, so page two is page two of the results
    rather than of everything, and the term is carried into the page links -
    without which paging away from page one silently drops the search.
    """
    term = (request.GET.get('q') or '').strip()
    results = queryset
    if term:
        condition = Q()
        for field in fields:
            condition |= Q(**{f'{field}__icontains': term})
        results = queryset.filter(condition).distinct()

    return {
        name: _paginate(request, results),
        'total_count': queryset.count(),
        'result_count': results.count() if term else None,
        'search_term': term,
        # Prefixed onto ?page=N in the pager, so it must end with its own &.
        'page_qs': f'q={quote(term)}&' if term else '',
    }

@login_required
def dashboard(request):
    """Two dashboards behind one URL.

    An administrator runs the department and needs its totals. A lecturer needs
    their own teaching, and department-wide counts are neither actionable nor
    theirs to see.
    """
    if is_admin(request.user):
        return render(request, 'scheduler/dashboard.html', {
            'total_courses': Course.objects.count(),
            'total_rooms': Room.objects.count(),
            'total_lecturers': Lecturer.objects.count(),
            'total_entries': TimetableEntry.objects.filter(is_active=True).count(),
            'pending_requests': RescheduleRequest.objects.filter(status='PENDING').count(),
            'conflict_count': len(detect_conflicts()),
        })

    student = student_for(request.user)
    if student is not None:
        my_classes = TimetableEntry.objects.filter(is_active=True)
        my_classes = (my_classes.filter(student_group=student.group)
                      if student.group else my_classes.none())
        return render(request, 'scheduler/dashboard_student.html', {
            'student': student,
            'my_class_count': my_classes.count(),
            'course_count': student.group.courses.count() if student.group else 0,
            'next_classes': my_classes.select_related(
                'course', 'room', 'timeslot', 'course__lecturer'
            ).order_by('timeslot__start_time')[:5],
            'recent_notifications': Notification.objects.filter(user=request.user)[:5],
        })

    lecturer = lecturer_for(request.user)
    my_entries = TimetableEntry.objects.filter(is_active=True)
    my_entries = my_entries.filter(course__lecturer=lecturer) if lecturer else my_entries.none()

    return render(request, 'scheduler/dashboard_lecturer.html', {
        'lecturer': lecturer,
        'my_class_count': my_entries.count(),
        'my_course_count': Course.objects.filter(lecturer=lecturer).count() if lecturer else 0,
        'my_pending_count': RescheduleRequest.objects.filter(
            requested_by=request.user, status='PENDING'
        ).count(),
        'next_classes': my_entries.select_related(
            'course', 'room', 'timeslot', 'student_group'
        ).order_by('timeslot__start_time')[:5],
    })
@login_required
def timetable_view(request):
    days = ['MON', 'TUE', 'WED', 'THU', 'FRI']
    day_names = {'MON':'Monday','TUE':'Tuesday','WED':'Wednesday','THU':'Thursday','FRI':'Friday'}
    entries = TimetableEntry.objects.filter(is_active=True).select_related(
        'course', 'room', 'timeslot', 'student_group', 'course__lecturer'
    )

    # A student's timetable is their own: locked to their programme and level,
    # with no filter controls, rather than the whole department's grid.
    student = student_for(request.user)
    if student is not None:
        entries = entries.filter(student_group=student.group) if student.group else entries.none()
        group_filter = lecturer_filter = None
    else:
        group_filter = request.GET.get('group')
        lecturer_filter = request.GET.get('lecturer')
        if group_filter:
            entries = entries.filter(student_group__id=group_filter)
        if lecturer_filter:
            entries = entries.filter(course__lecturer__id=lecturer_filter)

    # One row per day, one column per distinct period. A TimeSlot carries its
    # own day, so a row per TimeSlot would render a diagonal staircase in which
    # only one cell per row could ever be filled; grouping by period collapses
    # that into a real grid.
    periods = list(TimeSlot.objects
                   .values_list('start_time', 'end_time')
                   .distinct()
                   .order_by('start_time'))

    grid = {d: {p: [] for p in periods} for d in days}
    for entry in entries:
        period = (entry.timeslot.start_time, entry.timeslot.end_time)
        if entry.timeslot.day in grid and period in grid[entry.timeslot.day]:
            grid[entry.timeslot.day][period].append(entry)

    context = {
        'days': days,
        'day_names': day_names,
        'periods': [{'start': s, 'end': e} for s, e in periods],
        'grid': [
            {
                'day': day,
                'day_name': day_names[day],
                'cells': [grid[day][p] for p in periods],
            }
            for day in days
        ],
        # Students get no filter controls: the grid is already theirs, and the
        # dropdowns would list every group and every member of staff.
        'groups': [] if student else StudentGroup.objects.all(),
        'lecturers': [] if student else Lecturer.objects.all(),
        'selected_group': group_filter,
        'selected_lecturer': lecturer_filter,
        'student': student,
    }
    return render(request, 'scheduler/timetable.html', context)

@login_required
@staff_required
def conflict_view(request):
    conflicts = detect_conflicts()
    return render(request, 'scheduler/conflicts.html', {'conflicts': conflicts})

@login_required
@require_POST
def view_as(request, pk):
    """Start previewing the app as another account.

    admin_required is deliberately not used: the decorator would test the
    already-swapped user, so a preview could be chained from inside a preview.
    The real signed-in account is what decides.
    """
    real_user = request.impersonator or request.user
    if not is_admin(real_user):
        raise Http404

    target = get_object_or_404(get_user_model(), pk=pk)
    if is_admin(target):
        messages.error(
            request,
            'That account is an administrator. Previewing is only ever a step '
            'down in access, never a step up.',
        )
        return redirect(request.POST.get('next') or 'dashboard')

    request.session[IMPERSONATE_SESSION_KEY] = target.pk
    logger.info('%s started previewing as %s', real_user.username, target.username)
    return redirect('dashboard')

@login_required
@require_POST
def stop_view_as(request):
    real_user = request.impersonator
    request.session.pop(IMPERSONATE_SESSION_KEY, None)
    if real_user:
        logger.info('%s stopped previewing', real_user.username)
    return redirect(request.POST.get('next') or 'dashboard')

@login_required
def my_account(request):
    """Where someone maintains their own address and password.

    Without this the only route to an email address is an administrator typing
    it in, which leaves anyone missing one unable to reset their password and
    unable to fix that themselves.
    """
    profile = student_for(request.user) or lecturer_for(request.user)

    # Changing the password of an account you are merely previewing is not
    # something a preview should ever be able to do.
    previewing = request.impersonator is not None

    email_form = MyEmailForm(
        user=request.user, profile=profile,
        initial={'email': request.user.email},
    )
    password_form = PasswordChangeForm(user=request.user)

    if request.method == 'POST':
        if previewing:
            messages.error(
                request,
                'You are previewing as someone else. Return to your own account '
                'before changing anything here.',
            )
            return redirect('my_account')

        if 'save_email' in request.POST:
            email_form = MyEmailForm(
                request.POST, user=request.user, profile=profile,
            )
            if email_form.is_valid():
                saved = email_form.save()
                logger.info('%s changed their own email address', request.user.username)
                messages.success(
                    request,
                    f'Email address set to {saved}.' if saved
                    else 'Email address removed. You will not be able to reset '
                         'your own password until you add one.',
                )
                return redirect('my_account')

        elif 'save_password' in request.POST:
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                password_form.save()
                # Changing a password rotates the session hash, which would
                # otherwise sign the user out of the tab they are sitting in.
                update_session_auth_hash(request, password_form.user)
                logger.info('%s changed their own password', request.user.username)
                messages.success(request, 'Your password has been changed.')
                return redirect('my_account')

        elif 'test_email' in request.POST and is_admin(request.user):
            _send_test_email(request)
            return redirect('my_account')

    return render(request, 'scheduler/account.html', {
        'profile': profile,
        'email_form': email_form,
        'password_form': password_form,
        'previewing': previewing,
        'mail_is_configured': bool(settings.EMAIL_HOST),
        'mail_host': settings.EMAIL_HOST,
        'mail_port': settings.EMAIL_PORT,
        'mail_from': settings.DEFAULT_FROM_EMAIL,
    })


def _send_test_email(request):
    """Try one real send and report what happened, in full.

    Password resets are sent by a background code path whose failures only
    reach the log. On a host with no shell that log is awkward to get at, so
    this is the same send made deliberately, with the mail server's own words
    put on the screen - which is the difference between "no email arrived" and
    "the provider rejected the password".
    """
    if not request.user.email:
        messages.error(
            request,
            'Add your own email address first - there is nowhere to send it.',
        )
        return

    if not settings.EMAIL_HOST:
        messages.warning(
            request,
            'No mail server is configured, so password reset emails are written '
            'to the application log instead of being sent. Set EMAIL_HOST, '
            'EMAIL_PORT, EMAIL_HOST_USER and EMAIL_HOST_PASSWORD to send them.',
        )
        return

    try:
        send_mail(
            subject='KTS test email',
            message=(
                'This is a test from the KNUST Timetable System.\n\n'
                'If you are reading it, password reset emails will arrive too.'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[request.user.email],
            fail_silently=False,
        )
    except MAIL_DELIVERY_ERRORS as exc:
        logger.exception('Test email to %s failed', request.user.username)
        messages.error(
            request,
            f'The mail server refused the message: {exc.__class__.__name__}: {exc} '
            f'(host {settings.EMAIL_HOST}, port {settings.EMAIL_PORT}). '
            f'Password reset emails are failing the same way.',
        )
        return

    logger.info('%s sent themselves a test email', request.user.username)
    messages.success(
        request,
        f'Test email sent to {request.user.email}. If it does not arrive within '
        f'a few minutes, check the spam folder before changing any settings.',
    )

@login_required
def notification_list(request):
    """In-system delivery: the requirement asks for notifications, and a host
    with no outbound mail cannot honestly promise anything else."""
    notifications = Notification.objects.filter(user=request.user)
    return render(request, 'scheduler/notifications.html', {
        'notifications': _paginate(request, notifications),
        'unread_count': notifications.filter(read_at__isnull=True).count(),
    })

@login_required
@require_POST
def notifications_mark_read(request):
    updated = Notification.objects.filter(
        user=request.user, read_at__isnull=True
    ).update(read_at=timezone.now())
    if updated:
        messages.success(request, f'{updated} notification(s) marked as read.')
    return redirect('notifications')

# Emptying a table takes rows in other tables with it. Each entry names what
# else goes, and the counts are worked out live rather than described in prose,
# so the confirmation says what will actually happen to this database.
BULK_DELETE = {
    'lecturers': {
        'model': Lecturer, 'label': 'lecturers', 'redirect': 'lecturers',
    },
    'courses': {
        'model': Course, 'label': 'courses', 'redirect': 'courses',
    },
    'rooms': {
        'model': Room, 'label': 'rooms', 'redirect': 'rooms',
    },
    'student-groups': {
        'model': StudentGroup, 'label': 'student groups', 'redirect': 'studentgroups',
    },
    'students': {
        'model': Student, 'label': 'students', 'redirect': 'students',
    },
    'timeslots': {
        'model': TimeSlot, 'label': 'time slots', 'redirect': 'timeslots',
    },
}


def _bulk_delete_impact(kind):
    """What else disappears, counted against the data actually present."""
    entries = TimetableEntry.objects.count()
    requests = RescheduleRequest.objects.count()
    impact = []

    if kind in ('courses', 'rooms', 'timeslots', 'student-groups') and entries:
        impact.append(f'{entries} timetable entr(ies) - the whole timetable')
    if kind == 'timeslots' and requests:
        impact.append(f'{requests} reschedule request(s)')
    if kind == 'lecturers':
        affected = Course.objects.filter(lecturer__isnull=False).count()
        if affected:
            impact.append(f'{affected} course(s) will be left without a lecturer')
    if kind == 'student-groups':
        assigned = Student.objects.filter(group__isnull=False).count()
        if assigned:
            impact.append(f'{assigned} student(s) will be left without a group')
    if kind == 'students':
        accounts = Student.objects.filter(user__isnull=False).count()
        if accounts:
            impact.append(
                f'{accounts} login account(s) will remain but stop being attached '
                f'to a student, so they will show no timetable'
            )
    return impact


@login_required
@admin_required
def bulk_delete(request, kind):
    if kind not in BULK_DELETE:
        raise Http404
    spec = BULK_DELETE[kind]
    total = spec['model'].objects.count()

    if request.method == 'POST':
        if not total:
            messages.warning(request, f'There were no {spec["label"]} to delete.')
            return redirect(spec['redirect'])
        deleted, _ = spec['model'].objects.all().delete()
        logger.warning(
            '%s deleted all %s (%d records, %d rows in total)',
            request.user.username, kind, total, deleted,
        )
        messages.success(request, f'Deleted all {total} {spec["label"]}.')
        return redirect(spec['redirect'])

    return render(request, 'scheduler/bulk_delete_confirm.html', {
        'kind': kind,
        'label': spec['label'],
        'total': total,
        'impact': _bulk_delete_impact(kind),
        'back': spec['redirect'],
    })


@login_required
@admin_required
def data_import(request):
    kind = request.POST.get('kind') or request.GET.get('kind') or 'lecturers'
    if kind not in KINDS:
        raise Http404

    result = None
    if request.method == 'POST':
        upload = request.FILES.get('file')
        if upload is None:
            messages.error(request, 'Choose a CSV file to upload.')
        else:
            try:
                result = run_import(kind, upload)
            except CsvImportError as exc:
                messages.error(request, str(exc))
            else:
                logger.info(
                    '%s imported %s: %d rows -> %d created, %d updated, '
                    '%d repeated, %d skipped',
                    request.user.username, kind, result.rows_read,
                    result.created, result.updated, result.repeated,
                    len(result.skipped),
                )
                summary = (
                    f'{KINDS[kind]["label"]}: read {result.rows_read} row(s), '
                    f'{result.created} added, {result.updated} updated.'
                )
                if result.auto_created:
                    summary += f' {len(result.auto_created)} referenced record(s) created.'
                messages.success(request, summary)

                # The headline number people check is how many records exist
                # afterwards, and rows collapsing into each other is the only
                # thing that makes it smaller than the file. Say so plainly.
                if result.repeated:
                    column = result.identifier_column
                    examples = ', '.join(result.repeated_examples)
                    messages.warning(
                        request,
                        f'{result.repeated} row(s) repeated a '
                        f'{" + ".join(KINDS[kind]["key"])} used earlier in the file, so '
                        f'they overwrote each other instead of adding records. That is '
                        f'why {result.rows_read} rows became {result.records} record(s). '
                        f'The identifier was read from your "{column}" column'
                        f'{f" - repeated values include {examples}" if examples else ""}. '
                        f'If that is the wrong column, rename or remove it and import again.',
                    )

                if result.skipped:
                    messages.warning(
                        request,
                        f'{len(result.skipped)} row(s) were skipped - listed below. '
                        f'Everything else was imported.',
                    )
                elif not result.repeated:
                    return redirect(f'{reverse("data_import")}?kind={kind}')

    return render(request, 'scheduler/import.html', {
        'kinds': KINDS,
        'kind': kind,
        'spec': KINDS[kind],
        'result': result,
    })

@login_required
@admin_required
def data_import_template(request, kind):
    if kind not in KINDS:
        raise Http404
    response = HttpResponse(template_csv(kind), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="kts-{kind}-template.csv"'
    return response

@login_required
@admin_required
def student_list(request):
    students = Student.objects.select_related('group', 'user').order_by('student_id')
    return render(request, 'scheduler/students.html', _list_context(
        request, students,
        ['student_id', 'index_number', 'name', 'programme', 'level', 'group__name'],
        'students',
    ))

@login_required
@admin_required
def student_edit(request, pk=None):
    student = get_object_or_404(Student, pk=pk) if pk else None
    if request.method == 'POST':
        form = StudentForm(request.POST, instance=student)
        if form.is_valid():
            form.save()
            messages.success(request, f"Student {'updated' if pk else 'added'} successfully.")
            return redirect('students')
    else:
        form = StudentForm(instance=student)
    return render(request, 'scheduler/student_form.html', {'form': form, 'student': student})

@login_required
@admin_required
def student_delete(request, pk):
    student = get_object_or_404(Student, pk=pk)
    if request.method == 'POST':
        student.delete()
        messages.success(request, f'Student {student.student_id} deleted.')
        return redirect('students')
    return render(request, 'scheduler/student_confirm_delete.html', {'student': student})

@login_required
@admin_required
@require_POST
def student_create_account(request, pk):
    """The student signs in with their student ID, so that is the username."""
    student = get_object_or_404(Student, pk=pk)
    if student.user_id:
        messages.warning(request, f'{student.student_id} already has an account.')
        return redirect('students')

    User = get_user_model()
    if User.objects.filter(username=student.student_id).exists():
        messages.error(
            request,
            f'An account named "{student.student_id}" already exists but is not '
            f'linked to this student. Link it in the admin panel instead.',
        )
        return redirect('students')

    user, password = create_student_account(student)
    logger.info(
        'Student account %s created for %s by %s',
        user.username, student.name, request.user.username,
    )
    messages.success(
        request,
        f'Account created for {student.name} — student ID "{student.student_id}" '
        f'is the username, password "{password}". This password is shown once '
        f'and cannot be recovered; pass it on securely.',
    )
    return redirect('students')

@login_required
@admin_required
def generate_timetable(request):
    if request.method == 'POST':
        result = run_genetic_algorithm()
        if result['success']:
            GenerationRun.objects.create(
                generations_run=result['generations_run'],
                best_fitness=result['fitness'],
                entries_created=result['entries_created'],
                dropped=result['dropped'],
                runtime_seconds=result['runtime_seconds'],
                history=result['history'],
            )
            GenerationRun.prune()
            logger.info(
                'Timetable generated by %s: %d entries, %d dropped, fitness %.4f in %.2fs',
                request.user.username, result['entries_created'], result['dropped'],
                result['fitness'], result['runtime_seconds'],
            )
            # Regeneration replaces every entry, so every student's timetable
            # has potentially moved.
            notify_all_students(
                'The timetable has been regenerated. Your schedule may have '
                'changed - check your timetable.'
            )
            messages.success(request, f"Timetable generated successfully! {result['entries_created']} entries created.")
            if result.get('dropped'):
                messages.warning(request, result['message'])
        else:
            logger.warning(
                'Timetable generation failed for %s: %s',
                request.user.username, result['message'],
            )
            messages.error(request, f"Could not generate a conflict-free timetable: {result['message']}")
        return redirect('timetable')
    return render(request, 'scheduler/generate.html')

@login_required
@admin_required
def algorithm_report(request):
    """Evidence that the generator works: convergence, and how it compares to
    approaches that do not need a genetic algorithm at all."""
    latest = GenerationRun.objects.first()
    return render(request, 'scheduler/algorithm.html', {
        'latest': latest,
        'chart': convergence_chart(latest.history) if latest else None,
        'runs': GenerationRun.objects.all()[:10],
        'baseline': compare(trials=5),
        'weights': [
            ('Room double-booked', PENALTY_ROOM_CLASH,
             'Two classes cannot share a room. Unusable.'),
            ('Lecturer double-booked', PENALTY_LECTURER_CLASH,
             'One person cannot be in two rooms. Unusable.'),
            ('Student group clash', PENALTY_GROUP_CLASH,
             'Also impossible, but a group can be split across sessions in a '
             'way a room or a person cannot, so it is weighted lower.'),
            ('Room over capacity', PENALTY_OVER_CAPACITY,
             'The only soft constraint: workable, merely bad.'),
        ],
    })

def _requestable_entries(user):
    """The classes this account may raise a reschedule against.

    Administrators cover the whole timetable. Everyone else is limited to the
    classes taught by the lecturer their account is linked to - and an account
    with no lecturer linked gets nothing, rather than everything.
    """
    entries = TimetableEntry.objects.filter(is_active=True).select_related(
        'course', 'timeslot', 'room', 'course__lecturer'
    )
    if is_admin(user):
        return entries
    lecturer = lecturer_for(user)
    if lecturer is None:
        return entries.none()
    return entries.filter(course__lecturer=lecturer)


@login_required
@staff_required
def reschedule_request(request):
    # This queryset is the security boundary, not just the dropdown's contents:
    # the POST resolves the chosen entry against it, so a forged id 404s.
    entries = _requestable_entries(request.user)

    if request.method == 'POST':
        entry = get_object_or_404(entries, id=request.POST.get('entry'))
        timeslot = get_object_or_404(TimeSlot, id=request.POST.get('timeslot'))
        room_id = request.POST.get('room')
        room = get_object_or_404(Room, id=room_id) if room_id else entry.room
        RescheduleRequest.objects.create(
            entry=entry, requested_timeslot=timeslot,
            requested_room=room, reason=request.POST.get('reason'),
            requested_by=request.user,
        )
        messages.success(request, 'Reschedule request submitted successfully.')
        return redirect('timetable')

    everything = RescheduleRequest.objects.select_related(
        'entry__course', 'entry__room', 'requested_timeslot', 'requested_room',
        'requested_by', 'decided_by',
    )

    # Admins get a queue of what needs deciding. Everyone gets the history of
    # their own requests, including the decided ones - a requester who cannot
    # see the outcome has no way to learn it, since nothing emails them.
    pending = everything.filter(status='PENDING') if is_admin(request.user) else None
    my_requests = everything.filter(requested_by=request.user)[:20]

    return render(request, 'scheduler/reschedule.html', {
        'entries': entries,
        'timeslots': TimeSlot.objects.all(),
        'rooms': Room.objects.all(),
        'pending_requests': pending,
        'my_requests': my_requests,
        'lecturer_profile': lecturer_for(request.user),
    })

@login_required
@admin_required
@require_POST
def approve_reschedule(request, pk):
    req = get_object_or_404(RescheduleRequest, pk=pk)
    conflicts = detect_conflicts(
        exclude_entry=req.entry,
        check_timeslot=req.requested_timeslot,
        check_room=req.requested_room or req.entry.room
    )
    if conflicts:
        logger.info(
            'Reschedule %s refused for %s: would create %d conflict(s)',
            req.pk, request.user.username, len(conflicts),
        )
        messages.error(request, f'Cannot approve — this change creates {len(conflicts)} conflict(s).')
    else:
        req.entry.timeslot = req.requested_timeslot
        # requested_room is nullable; keep the current room when none was requested
        req.entry.room = req.requested_room or req.entry.room
        req.entry.save()
        req.status = 'APPROVED'
        req.decided_by = request.user
        req.decided_at = timezone.now()
        req.save()
        logger.info(
            'Reschedule %s approved by %s: %s moved to %s',
            req.pk, request.user.username, req.entry.course.code, req.requested_timeslot,
        )
        # Only the group whose class actually moved is affected.
        notify_group_of_change(
            req.entry.student_group,
            f'{req.entry.course.code} ({req.entry.course.name}) has moved to '
            f'{req.entry.timeslot} in {req.entry.room.name}.',
        )
        # And close the loop for whoever asked, so they are not left checking.
        if req.requested_by and req.requested_by != request.user:
            notify(
                [req.requested_by],
                f'Your reschedule request for {req.entry.course.code} was approved. '
                f'It now runs {req.entry.timeslot} in {req.entry.room.name}.',
            )
        messages.success(request, 'Reschedule approved and timetable updated.')
    return redirect('reschedule')

@login_required
@admin_required
@require_POST
def reject_reschedule(request, pk):
    req = get_object_or_404(RescheduleRequest, pk=pk)
    req.status = 'REJECTED'
    req.decided_by = request.user
    req.decided_at = timezone.now()
    req.save()
    logger.info('Reschedule %s rejected by %s', req.pk, request.user.username)
    if req.requested_by and req.requested_by != request.user:
        notify(
            [req.requested_by],
            f'Your reschedule request for {req.entry.course.code} was not approved. '
            f'It still runs {req.entry.timeslot} in {req.entry.room.name}.',
        )
    messages.success(request, 'Reschedule request rejected.')
    return redirect('reschedule')

@login_required
@admin_required
def lecturer_list(request):
    lecturers = Lecturer.objects.select_related('user').order_by('name')
    return render(request, 'scheduler/lecturers.html', _list_context(
        request, lecturers, ['name', 'email', 'user__username'], 'lecturers',
    ))

@login_required
@admin_required
def lecturer_edit(request, pk=None):
    lecturer = get_object_or_404(Lecturer, pk=pk) if pk else None
    if request.method == 'POST':
        form = LecturerForm(request.POST, instance=lecturer)
        if form.is_valid():
            form.save()
            messages.success(request, f"Lecturer {'updated' if pk else 'added'} successfully.")
            return redirect('lecturers')
    else:
        form = LecturerForm(instance=lecturer)
    return render(request, 'scheduler/lecturer_form.html', {'form': form, 'lecturer': lecturer})

@login_required
@admin_required
def course_list(request):
    courses = Course.objects.select_related('lecturer').order_by('code')
    return render(request, 'scheduler/courses.html', _list_context(
        request, courses, ['code', 'name', 'lecturer__name', 'lecturer__email'],
        'courses',
    ))

@login_required
@admin_required
def course_edit(request, pk=None):
    course = get_object_or_404(Course, pk=pk) if pk else None
    if request.method == 'POST':
        form = CourseForm(request.POST, instance=course)
        if form.is_valid():
            form.save()
            messages.success(request, f"Course {'updated' if pk else 'added'} successfully.")
            return redirect('courses')
    else:
        form = CourseForm(instance=course)
    return render(request, 'scheduler/course_form.html', {'form': form, 'course': course})

@login_required
@admin_required
@require_POST
def lecturer_create_account(request, pk):
    """Give a lecturer a login without needing shell access to the server.

    The generated password is shown to the administrator once, in a message.
    It is never stored in readable form and never written to the log.
    """
    lecturer = get_object_or_404(Lecturer, pk=pk)
    if lecturer.user_id:
        messages.warning(request, f'{lecturer.name} already has an account.')
        return redirect('lecturers')

    User = get_user_model()
    taken = set(User.objects.values_list('username', flat=True))
    username = derive_username(lecturer.email, taken)
    password = generate_password()

    user = User.objects.create_user(
        username=username, email=lecturer.email, password=password
    )
    lecturer.user = user
    lecturer.save(update_fields=['user'])

    logger.info(
        'Login account %s created for lecturer %s by %s',
        username, lecturer.name, request.user.username,
    )
    messages.success(
        request,
        f'Account created for {lecturer.name} — username "{username}", '
        f'password "{password}". This password is shown once and cannot be '
        f'recovered; pass it on securely and have them change it.',
    )
    return redirect('lecturers')

@login_required
@admin_required
def lecturer_delete(request, pk):
    lecturer = get_object_or_404(Lecturer, pk=pk)
    if request.method == 'POST':
        lecturer.delete()
        messages.success(request, f"Lecturer {lecturer.name} deleted.")
        return redirect('lecturers')
    return render(request, 'scheduler/lecturer_confirm_delete.html', {'lecturer': lecturer})

@login_required
@admin_required
def course_delete(request, pk):
    course = get_object_or_404(Course, pk=pk)
    if request.method == 'POST':
        course.delete()
        messages.success(request, f"Course {course.code} deleted.")
        return redirect('courses')
    return render(request, 'scheduler/course_confirm_delete.html', {'course': course})

@login_required
@admin_required
def room_list(request):
    rooms = Room.objects.all().order_by('name')
    return render(request, 'scheduler/rooms.html', _list_context(
        request, rooms, ['name'], 'rooms',
    ))

@login_required
@admin_required
def room_edit(request, pk=None):
    room = get_object_or_404(Room, pk=pk) if pk else None
    if request.method == 'POST':
        form = RoomForm(request.POST, instance=room)
        if form.is_valid():
            form.save()
            messages.success(request, f"Room {'updated' if pk else 'added'} successfully.")
            return redirect('rooms')
    else:
        form = RoomForm(instance=room)
    return render(request, 'scheduler/room_form.html', {'form': form, 'room': room})

@login_required
@admin_required
def room_delete(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if request.method == 'POST':
        room.delete()
        messages.success(request, f"Room {room.name} deleted.")
        return redirect('rooms')
    return render(request, 'scheduler/room_confirm_delete.html', {'room': room})

@login_required
@admin_required
def studentgroup_list(request):
    groups = (StudentGroup.objects
              .order_by('name')
              .prefetch_related('courses')
              .annotate(enrolled_count=Count('students')))
    return render(request, 'scheduler/studentgroups.html', _list_context(
        request, groups, ['name', 'courses__code', 'courses__name'], 'groups',
    ))

@login_required
@admin_required
def studentgroup_edit(request, pk=None):
    group = get_object_or_404(StudentGroup, pk=pk) if pk else None
    if request.method == 'POST':
        form = StudentGroupForm(request.POST, instance=group)
        if form.is_valid():
            form.save()
            messages.success(request, f"Student group {'updated' if pk else 'added'} successfully.")
            return redirect('studentgroups')
    else:
        form = StudentGroupForm(instance=group)
    return render(request, 'scheduler/studentgroup_form.html', {'form': form, 'group': group})

@login_required
@admin_required
def studentgroup_delete(request, pk):
    group = get_object_or_404(StudentGroup, pk=pk)
    if request.method == 'POST':
        group.delete()
        messages.success(request, f"Student group {group.name} deleted.")
        return redirect('studentgroups')
    return render(request, 'scheduler/studentgroup_confirm_delete.html', {'group': group})

@login_required
@admin_required
def timeslot_list(request):
    # Model Meta already orders these Monday-first; order_by('day') would sort
    # the raw codes alphabetically and put Friday at the top.
    timeslots = TimeSlot.objects.all()
    return render(request, 'scheduler/timeslots.html', {
        'timeslots': _paginate(request, timeslots),
        'total_count': timeslots.count(),
    })

@login_required
@admin_required
def timeslot_edit(request, pk=None):
    timeslot = get_object_or_404(TimeSlot, pk=pk) if pk else None
    if request.method == 'POST':
        form = TimeSlotForm(request.POST, instance=timeslot)
        if form.is_valid():
            form.save()
            messages.success(request, f"Time slot {'updated' if pk else 'added'} successfully.")
            return redirect('timeslots')
    else:
        form = TimeSlotForm(instance=timeslot)
    return render(request, 'scheduler/timeslot_form.html', {'form': form, 'timeslot': timeslot})

@login_required
@admin_required
def timeslot_delete(request, pk):
    timeslot = get_object_or_404(TimeSlot, pk=pk)
    if request.method == 'POST':
        timeslot.delete()
        messages.success(request, 'Time slot deleted.')
        return redirect('timeslots')
    return render(request, 'scheduler/timeslot_confirm_delete.html', {'timeslot': timeslot})