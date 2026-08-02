from django.db import models

class Room(models.Model):
    name = models.CharField(max_length=100)
    capacity = models.IntegerField()

    def __str__(self):
        return f"{self.name} (cap: {self.capacity})"

class Lecturer(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)

    def __str__(self):
        return self.name

class Course(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    expected_students = models.IntegerField(default=30)
    lecturer = models.ForeignKey(Lecturer, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.code} - {self.name}"

class StudentGroup(models.Model):
    name = models.CharField(max_length=100)
    courses = models.ManyToManyField(Course, blank=True)

    def __str__(self):
        return self.name

class TimeSlot(models.Model):
    DAYS = [
        ('MON', 'Monday'), ('TUE', 'Tuesday'), ('WED', 'Wednesday'),
        ('THU', 'Thursday'), ('FRI', 'Friday'),
    ]
    day = models.CharField(max_length=3, choices=DAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.day} {self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"

class TimetableEntry(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)
    student_group = models.ForeignKey(StudentGroup, on_delete=models.CASCADE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['room', 'timeslot'],
                condition=models.Q(is_active=True),
                name='unique_active_room_timeslot',
            )
        ]

    def __str__(self):
        return f"{self.course.code} | {self.room.name} | {self.timeslot}"

class RescheduleRequest(models.Model):
    STATUS = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    entry = models.ForeignKey(TimetableEntry, on_delete=models.CASCADE)
    requested_timeslot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE, related_name='reschedule_requests')
    requested_room = models.ForeignKey(Room, on_delete=models.CASCADE, null=True, blank=True)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Request for {self.entry.course.code} - {self.status}"