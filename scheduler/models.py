from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

DAY_CHOICES = [
    ('MON', 'Monday'), ('TUE', 'Tuesday'), ('WED', 'Wednesday'),
    ('THU', 'Thursday'), ('FRI', 'Friday'),
]


def day_ordering():
    """Sort Monday to Friday.

    Ordering on the raw code sorts alphabetically - FRI, MON, THU, TUE, WED -
    which is never what anyone reading a timetable wants.
    """
    return models.Case(
        *[models.When(day=code, then=index)
          for index, (code, _label) in enumerate(DAY_CHOICES)],
        output_field=models.IntegerField(),
    )

class Room(models.Model):
    name = models.CharField(max_length=100, unique=True)
    capacity = models.IntegerField()

    class Meta:
        ordering = ['name']

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
    name = models.CharField(max_length=100, unique=True)
    courses = models.ManyToManyField(Course, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class TimeSlot(models.Model):
    DAYS = DAY_CHOICES

    day = models.CharField(max_length=3, choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = [day_ordering(), 'start_time']
        constraints = [
            # A duplicated slot does not just clutter the list: the timetable
            # groups rows by distinct (start, end), so entries on the duplicate
            # silently vanish from the grid while still existing in the database.
            models.UniqueConstraint(
                fields=['day', 'start_time', 'end_time'],
                name='unique_day_start_end',
            )
        ]

    def clean(self):
        super().clean()
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({'end_time': 'End time must be after start time.'})

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

class GenerationRun(models.Model):
    """One execution of the generator, kept so the algorithm can be evidenced.

    A genetic algorithm is only defensible if you can show it converging and
    show it beating a naive baseline; without a record of each run there is
    nothing to plot and nothing to compare.
    """
    created_at = models.DateTimeField(auto_now_add=True)
    generations_run = models.IntegerField()
    best_fitness = models.FloatField()
    entries_created = models.IntegerField()
    dropped = models.IntegerField(default=0)
    runtime_seconds = models.FloatField()
    # Best fitness at the end of each generation, oldest first.
    history = models.JSONField(default=list)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Run {self.created_at:%Y-%m-%d %H:%M} - fitness {self.best_fitness:.4f}"

    @property
    def converged_at(self):
        """The first generation that reached the best fitness this run found."""
        for index, value in enumerate(self.history):
            if value >= self.best_fitness:
                return index + 1
        return self.generations_run


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
    # Nullable so historic rows survive, and so a request outlives the account
    # that raised it. Every request created through the app records its author.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reschedule_requests',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reschedule_decisions',
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Request for {self.entry.course.code} - {self.status}"