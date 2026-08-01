from django.contrib import admin
from .models import Room, Lecturer, Course, StudentGroup, TimeSlot, TimetableEntry, RescheduleRequest

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'capacity']
    search_fields = ['name']

@admin.register(Lecturer)
class LecturerAdmin(admin.ModelAdmin):
    list_display = ['name', 'email']
    search_fields = ['name', 'email']

@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'lecturer', 'expected_students']
    search_fields = ['code', 'name']

@admin.register(StudentGroup)
class StudentGroupAdmin(admin.ModelAdmin):
    list_display = ['name']
    filter_horizontal = ['courses']

@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ['day', 'start_time', 'end_time']
    ordering = ['day', 'start_time']

@admin.register(TimetableEntry)
class TimetableEntryAdmin(admin.ModelAdmin):
    list_display = ['course', 'room', 'timeslot', 'student_group', 'is_active']
    list_filter = ['is_active', 'timeslot__day']
    search_fields = ['course__code', 'room__name']

@admin.register(RescheduleRequest)
class RescheduleRequestAdmin(admin.ModelAdmin):
    list_display = ['entry', 'requested_timeslot', 'status', 'created_at']
    list_filter = ['status']