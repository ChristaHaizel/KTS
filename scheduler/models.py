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
    # Nullable: a lecturer is primarily scheduling data, and most of them will
    # never need to sign in. Linking one to an account is what lets them see
    # their own classes and raise requests against them.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lecturer_profile',
        help_text='The login account this lecturer signs in with, if they have one.',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

class Course(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    expected_students = models.IntegerField(default=30)
    # blank=True as well as null=True: SET_NULL means deleting a lecturer leaves
    # their courses unassigned, and without blank the form then refuses to save
    # those courses at all until someone is assigned - so the delete produces
    # rows that cannot be edited.
    lecturer = models.ForeignKey(
        Lecturer, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"{self.code} - {self.name}"

class StudentGroup(models.Model):
    """A teaching group - the unit a timetable is actually built for.

    A cohort too large for any room is split across several of these, and each
    one is scheduled separately, so Group 1 and Group 2 sit different
    timetables for the same courses.
    """
    name = models.CharField(max_length=100, unique=True)
    courses = models.ManyToManyField(Course, blank=True)
    # How many students sit in this group. The whole point of splitting a
    # cohort is that the parts fit rooms the whole does not, so a class must be
    # sized against the group attending it rather than against the course's
    # total enrolment.
    size = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=(
            'How many students are in this group. Leave blank to count the '
            'students assigned to it, or fall back to the course total.'
        ),
    )

    class Meta:
        ordering = ['name']

    def size_for(self, course):
        """How many people this class actually has to seat.

        The declared size wins, then the students on file, and only failing
        both does it fall back to the course's total - which is the whole
        cohort and therefore an overestimate for any group that is a split of
        one.
        """
        if self.size:
            return self.size
        # Uses the annotation when the caller supplied one, so the generator
        # does not run a count per gene per generation.
        counted = getattr(self, 'enrolled_count', None)
        if counted is None:
            counted = self.students.count()
        return counted or course.expected_students

    def __str__(self):
        return self.name

class Student(models.Model):
    LEVELS = [
        ('100', 'Level 100'), ('200', 'Level 200'), ('300', 'Level 300'),
        ('400', 'Level 400'), ('500', 'Level 500'), ('600', 'Level 600'),
    ]

    # The student ID is the credential: an account created for a student uses it
    # as the username, so they sign in with the number they already know.
    student_id = models.CharField(max_length=20, unique=True)
    # A separate identifier a student also carries, used on exam scripts. Unique
    # where given, but nullable, because not every record will have one to hand -
    # and several NULLs do not collide with each other.
    index_number = models.CharField(
        max_length=20, unique=True, null=True, blank=True,
        help_text='Their examination index number, if different from the student ID.',
    )
    name = models.CharField(max_length=100)
    programme = models.CharField(
        max_length=120, blank=True,
        help_text='For example, BSc Computer Science.',
    )
    level = models.CharField(max_length=10, choices=LEVELS, blank=True)
    # Descriptive fields above; this is the one that does work. The timetable is
    # built per group, so two programmes can share a teaching group without
    # either losing its own programme and level.
    group = models.ForeignKey(
        StudentGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='students',
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='student_profile',
        help_text='The login account this student signs in with, if they have one.',
    )

    class Meta:
        ordering = ['student_id']

    def save(self, *args, **kwargs):
        # A form posts "" for an untouched optional field, and two empty strings
        # collide on a unique constraint where two NULLs do not. NULL is the
        # honest representation of "no index number on file".
        if not self.index_number:
            self.index_number = None
        super().save(*args, **kwargs)

    @property
    def programme_and_level(self):
        return ' '.join(p for p in (self.programme, self.get_level_display()) if p)

    def __str__(self):
        return f"{self.student_id} - {self.name}"


class Notification(models.Model):
    """An in-system message. No email: the requirement is in-system delivery,
    and a free host with no outbound mail could not honour anything else."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The unread badge runs on every page load for every signed-in user.
            models.Index(fields=['user', 'read_at']),
        ]

    def __str__(self):
        state = 'read' if self.read_at else 'unread'
        return f"{self.user} - {state} - {self.message[:40]}"

    @property
    def is_unread(self):
        return self.read_at is None


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

    # Only the most recent runs are ever displayed, and each row carries up to
    # GENERATIONS floats, so the table is trimmed rather than grown forever.
    KEEP_RUNS = 50

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Run {self.created_at:%Y-%m-%d %H:%M} - fitness {self.best_fitness:.4f}"

    @classmethod
    def prune(cls):
        """Delete all but the most recent KEEP_RUNS rows."""
        keep = list(cls.objects.values_list('pk', flat=True)[:cls.KEEP_RUNS])
        deleted, _ = cls.objects.exclude(pk__in=keep).delete()
        return deleted

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