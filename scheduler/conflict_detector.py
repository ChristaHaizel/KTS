from .models import TimetableEntry

def detect_conflicts(exclude_entry=None, check_timeslot=None, check_room=None):
    conflicts = []
    entries = TimetableEntry.objects.filter(is_active=True).select_related(
        'course', 'room', 'timeslot', 'student_group', 'course__lecturer'
    )

    if check_timeslot and check_room:
        # Validating a proposed move. Checking only the room lets an approval
        # create exactly the lecturer and student-group clashes the generator
        # exists to prevent, while reporting the move as safe.
        at_that_time = entries.filter(timeslot=check_timeslot)
        if exclude_entry:
            at_that_time = at_that_time.exclude(id=exclude_entry.id)

        for e in at_that_time.filter(room=check_room):
            conflicts.append({
                'type': 'Room Conflict',
                'description': f'{check_room.name} is already booked at {check_timeslot} by {e.course.code}',
                'severity': 'high'
            })

        if exclude_entry:
            lecturer = exclude_entry.course.lecturer
            if lecturer:
                for e in at_that_time.filter(course__lecturer=lecturer):
                    conflicts.append({
                        'type': 'Lecturer Conflict',
                        'description': f'{lecturer.name} already teaches {e.course.code} at {check_timeslot}',
                        'severity': 'high'
                    })

            for e in at_that_time.filter(student_group=exclude_entry.student_group):
                conflicts.append({
                    'type': 'Student Group Conflict',
                    'description': f'{exclude_entry.student_group.name} already has {e.course.code} at {check_timeslot}',
                    'severity': 'medium'
                })

            if exclude_entry.course.expected_students > check_room.capacity:
                conflicts.append({
                    'type': 'Room Capacity Mismatch',
                    'description': f'{check_room.name} (capacity {check_room.capacity}) is too small for '
                                   f'{exclude_entry.course.code} (expected {exclude_entry.course.expected_students})',
                    'severity': 'low'
                })

        return conflicts

    seen_room_slots = {}
    seen_lecturer_slots = {}
    seen_group_slots = {}

    for entry in entries:
        slot_key = (entry.timeslot.id, entry.room.id)
        if slot_key in seen_room_slots:
            conflicts.append({
                'type': 'Room Conflict',
                'description': f'{entry.room.name} is double-booked at {entry.timeslot}: {seen_room_slots[slot_key].course.code} and {entry.course.code}',
                'severity': 'high'
            })
        else:
            seen_room_slots[slot_key] = entry

        if entry.course.lecturer:
            lecturer_key = (entry.timeslot.id, entry.course.lecturer.id)
            if lecturer_key in seen_lecturer_slots:
                conflicts.append({
                    'type': 'Lecturer Conflict',
                    'description': f'{entry.course.lecturer.name} is assigned to two classes at {entry.timeslot}: {seen_lecturer_slots[lecturer_key].course.code} and {entry.course.code}',
                    'severity': 'high'
                })
            else:
                seen_lecturer_slots[lecturer_key] = entry

        group_key = (entry.timeslot.id, entry.student_group.id)
        if group_key in seen_group_slots:
            conflicts.append({
                'type': 'Student Group Conflict',
                'description': f'{entry.student_group.name} has overlapping classes at {entry.timeslot}: {seen_group_slots[group_key].course.code} and {entry.course.code}',
                'severity': 'medium'
            })
        else:
            seen_group_slots[group_key] = entry

        if entry.room.capacity < entry.course.expected_students:
            conflicts.append({
                'type': 'Room Capacity Mismatch',
                'description': f'{entry.room.name} (capacity {entry.room.capacity}) is too small for {entry.course.code} (expected {entry.course.expected_students} students)',
                'severity': 'low'
            })

    return conflicts