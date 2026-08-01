from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import TimetableEntry, RescheduleRequest, Room, Lecturer, Course, StudentGroup, TimeSlot
from .conflict_detector import detect_conflicts
from .genetic_algorithm import run_genetic_algorithm
from .forms import LecturerForm, CourseForm, RoomForm, StudentGroupForm, TimeSlotForm

@login_required
def dashboard(request):
    total_courses = Course.objects.count()
    total_rooms = Room.objects.count()
    total_lecturers = Lecturer.objects.count()
    total_entries = TimetableEntry.objects.filter(is_active=True).count()
    pending_requests = RescheduleRequest.objects.filter(status='PENDING').count()
    conflicts = detect_conflicts()
    context = {
        'total_courses': total_courses,
        'total_rooms': total_rooms,
        'total_lecturers': total_lecturers,
        'total_entries': total_entries,
        'pending_requests': pending_requests,
        'conflict_count': len(conflicts),
    }
    return render(request, 'scheduler/dashboard.html', context)
@login_required
def timetable_view(request):
    days = ['MON', 'TUE', 'WED', 'THU', 'FRI']
    day_names = {'MON':'Monday','TUE':'Tuesday','WED':'Wednesday','THU':'Thursday','FRI':'Friday'}
    timeslots = TimeSlot.objects.all().order_by('day', 'start_time')
    entries = TimetableEntry.objects.filter(is_active=True).select_related(
        'course', 'room', 'timeslot', 'student_group', 'course__lecturer'
    )
    group_filter = request.GET.get('group')
    lecturer_filter = request.GET.get('lecturer')
    if group_filter:
        entries = entries.filter(student_group__id=group_filter)
    if lecturer_filter:
        entries = entries.filter(course__lecturer__id=lecturer_filter)
    context = {
        'days': days,
        'day_names': day_names,
        'timeslots': timeslots,
        'entries_list': list(entries),
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
def generate_timetable(request):
    if request.method == 'POST':
        result = run_genetic_algorithm()
        if result['success']:
            messages.success(request, f"Timetable generated successfully! {result['entries_created']} entries created.")
        else:
            messages.error(request, f"Could not generate a conflict-free timetable: {result['message']}")
        return redirect('timetable')
    return render(request, 'scheduler/generate.html')

@login_required
def reschedule_request(request):
    if request.method == 'POST':
        entry_id = request.POST.get('entry')
        timeslot_id = request.POST.get('timeslot')
        room_id = request.POST.get('room')
        reason = request.POST.get('reason')
        entry = get_object_or_404(TimetableEntry, id=entry_id)
        timeslot = get_object_or_404(TimeSlot, id=timeslot_id)
        room = get_object_or_404(Room, id=room_id) if room_id else entry.room
        RescheduleRequest.objects.create(
            entry=entry, requested_timeslot=timeslot,
            requested_room=room, reason=reason
        )
        messages.success(request, 'Reschedule request submitted successfully.')
        return redirect('timetable')
    entries = TimetableEntry.objects.filter(is_active=True).select_related('course', 'timeslot', 'room')
    timeslots = TimeSlot.objects.all()
    rooms = Room.objects.all()
    pending = RescheduleRequest.objects.filter(status='PENDING').select_related('entry__course', 'requested_timeslot')
    return render(request, 'scheduler/reschedule.html', {
        'entries': entries, 'timeslots': timeslots,
        'rooms': rooms, 'pending_requests': pending
    })

@login_required
def approve_reschedule(request, pk):
    req = get_object_or_404(RescheduleRequest, pk=pk)
    conflicts = detect_conflicts(
        exclude_entry=req.entry,
        check_timeslot=req.requested_timeslot,
        check_room=req.requested_room
    )
    if conflicts:
        messages.error(request, f'Cannot approve — this change creates {len(conflicts)} conflict(s).')
    else:
        req.entry.timeslot = req.requested_timeslot
        req.entry.room = req.requested_room
        req.entry.save()
        req.status = 'APPROVED'
        req.save()
        messages.success(request, 'Reschedule approved and timetable updated.')
    return redirect('reschedule')

@login_required
def reject_reschedule(request, pk):
    req = get_object_or_404(RescheduleRequest, pk=pk)
    req.status = 'REJECTED'
    req.save()
    messages.success(request, 'Reschedule request rejected.')
    return redirect('reschedule')

@login_required
def lecturer_list(request):
    lecturers = Lecturer.objects.all().order_by('name')
    return render(request, 'scheduler/lecturers.html', {'lecturers': lecturers})

@login_required
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
def course_list(request):
    courses = Course.objects.select_related('lecturer').order_by('code')
    return render(request, 'scheduler/courses.html', {'courses': courses})

@login_required
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
def lecturer_delete(request, pk):
    lecturer = get_object_or_404(Lecturer, pk=pk)
    if request.method == 'POST':
        lecturer.delete()
        messages.success(request, f"Lecturer {lecturer.name} deleted.")
        return redirect('lecturers')
    return render(request, 'scheduler/lecturer_confirm_delete.html', {'lecturer': lecturer})

@login_required
def course_delete(request, pk):
    course = get_object_or_404(Course, pk=pk)
    if request.method == 'POST':
        course.delete()
        messages.success(request, f"Course {course.code} deleted.")
        return redirect('courses')
    return render(request, 'scheduler/course_confirm_delete.html', {'course': course})

@login_required
def room_list(request):
    rooms = Room.objects.all().order_by('name')
    return render(request, 'scheduler/rooms.html', {'rooms': rooms})

@login_required
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
def room_delete(request, pk):
    room = get_object_or_404(Room, pk=pk)
    if request.method == 'POST':
        room.delete()
        messages.success(request, f"Room {room.name} deleted.")
        return redirect('rooms')
    return render(request, 'scheduler/room_confirm_delete.html', {'room': room})

@login_required
def studentgroup_list(request):
    groups = StudentGroup.objects.all().order_by('name').prefetch_related('courses')
    return render(request, 'scheduler/studentgroups.html', {'groups': groups})

@login_required
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
def studentgroup_delete(request, pk):
    group = get_object_or_404(StudentGroup, pk=pk)
    if request.method == 'POST':
        group.delete()
        messages.success(request, f"Student group {group.name} deleted.")
        return redirect('studentgroups')
    return render(request, 'scheduler/studentgroup_confirm_delete.html', {'group': group})

@login_required
def timeslot_list(request):
    timeslots = TimeSlot.objects.all().order_by('day', 'start_time')
    return render(request, 'scheduler/timeslots.html', {'timeslots': timeslots})

@login_required
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
def timeslot_delete(request, pk):
    timeslot = get_object_or_404(TimeSlot, pk=pk)
    if request.method == 'POST':
        timeslot.delete()
        messages.success(request, 'Time slot deleted.')
        return redirect('timeslots')
    return render(request, 'scheduler/timeslot_confirm_delete.html', {'timeslot': timeslot})