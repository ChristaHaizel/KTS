from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.views.decorators.http import require_POST
from .permissions import admin_required, is_admin, lecturer_for
from .models import (
    TimetableEntry, RescheduleRequest, Room, Lecturer, Course, StudentGroup,
    TimeSlot, GenerationRun,
)
from .baselines import compare
from .charts import convergence_chart
from .conflict_detector import detect_conflicts
from .genetic_algorithm import (
    run_genetic_algorithm, PENALTY_ROOM_CLASH, PENALTY_LECTURER_CLASH,
    PENALTY_GROUP_CLASH, PENALTY_OVER_CAPACITY,
)
from .forms import LecturerForm, CourseForm, RoomForm, StudentGroupForm, TimeSlotForm

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
    group_filter = request.GET.get('group')
    lecturer_filter = request.GET.get('lecturer')
    if group_filter:
        entries = entries.filter(student_group__id=group_filter)
    if lecturer_filter:
        entries = entries.filter(course__lecturer__id=lecturer_filter)

    # Rows are distinct periods (start/end times); days are the columns. A TimeSlot
    # already carries its own day, so one row per TimeSlot would render a diagonal
    # staircase where only one day column per row could ever be filled.
    periods = (TimeSlot.objects
               .values_list('start_time', 'end_time')
               .distinct()
               .order_by('start_time'))

    grid = {(start, end): {d: [] for d in days} for start, end in periods}
    for entry in entries:
        key = (entry.timeslot.start_time, entry.timeslot.end_time)
        if key in grid:
            grid[key][entry.timeslot.day].append(entry)

    context = {
        'days': days,
        'day_names': day_names,
        'grid': [
            {'start': s, 'end': e, 'cells': [grid[(s, e)][d] for d in days]}
            for s, e in periods
        ],
        'groups': StudentGroup.objects.all(),
        'lecturers': Lecturer.objects.all(),
        'selected_group': group_filter,
        'selected_lecturer': lecturer_filter,
    }
    return render(request, 'scheduler/timetable.html', context)

@login_required
def conflict_view(request):
    conflicts = detect_conflicts()
    return render(request, 'scheduler/conflicts.html', {'conflicts': conflicts})

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
            messages.success(request, f"Timetable generated successfully! {result['entries_created']} entries created.")
            if result.get('dropped'):
                messages.warning(request, result['message'])
        else:
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

    pending = RescheduleRequest.objects.filter(status='PENDING').select_related(
        'entry__course', 'requested_timeslot', 'requested_by'
    )
    if not is_admin(request.user):
        pending = pending.filter(requested_by=request.user)

    return render(request, 'scheduler/reschedule.html', {
        'entries': entries,
        'timeslots': TimeSlot.objects.all(),
        'rooms': Room.objects.all(),
        'pending_requests': pending,
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
    messages.success(request, 'Reschedule request rejected.')
    return redirect('reschedule')

@login_required
@admin_required
def lecturer_list(request):
    lecturers = Lecturer.objects.all().order_by('name')
    return render(request, 'scheduler/lecturers.html', {'lecturers': lecturers})

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
    return render(request, 'scheduler/courses.html', {'courses': courses})

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
    return render(request, 'scheduler/rooms.html', {'rooms': rooms})

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
    groups = StudentGroup.objects.all().order_by('name').prefetch_related('courses')
    return render(request, 'scheduler/studentgroups.html', {'groups': groups})

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
    timeslots = TimeSlot.objects.all().order_by('day', 'start_time')
    return render(request, 'scheduler/timeslots.html', {'timeslots': timeslots})

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