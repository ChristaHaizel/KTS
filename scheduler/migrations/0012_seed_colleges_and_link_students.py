"""Seed KNUST's colleges, and link students to the department they are already in.

Programme and department were always the same thing under two names. Every
student already carries a programme as text - "BSc Computer Science" - so the
departments are derived from what is on the roster rather than invented, and
each student is attached to the one their programme names.

Only the College of Science is listed out here, because that is the one this
system runs in. The other five exist so a student choosing from the dropdown
sees the university they are actually at, and the timetable office can fill in
their departments without waiting for a deploy.
"""
from django.db import migrations

COLLEGES = [
    'College of Agriculture and Natural Resources',
    'College of Art and Built Environment',
    'College of Engineering',
    'College of Health Sciences',
    'College of Humanities and Social Sciences',
    'College of Science',
]

SCIENCE_DEPARTMENTS = [
    'Computer Science',
    'Mathematics',
    'Physics',
    'Chemistry',
    'Biochemistry and Biotechnology',
    'Food Science and Technology',
    'Environmental Science',
    'Theoretical and Applied Biology',
    'Optometry and Visual Science',
]


def seed(apps, schema_editor):
    College = apps.get_model('scheduler', 'College')
    Department = apps.get_model('scheduler', 'Department')
    Student = apps.get_model('scheduler', 'Student')

    colleges = {}
    for name in COLLEGES:
        colleges[name] = College.objects.get_or_create(name=name)[0]

    science = colleges['College of Science']
    for name in SCIENCE_DEPARTMENTS:
        Department.objects.get_or_create(name=name, college=science)

    # Anything on the roster that is not one of the above is still a real
    # programme somebody is enrolled on, so it becomes a department too. Left
    # under the College of Science because that is the college this system
    # serves; the timetable office can move it.
    known = {d.name.lower(): d for d in Department.objects.all()}
    for programme in (Student.objects
                      .exclude(programme='')
                      .values_list('programme', flat=True)
                      .distinct()):
        # "BSc Computer Science" names the Department of Computer Science.
        match = next((d for name, d in known.items() if name in programme.lower()), None)
        if match is None:
            match = Department.objects.create(name=programme, college=science)
            known[programme.lower()] = match

    for student in Student.objects.exclude(programme=''):
        match = next(
            (d for name, d in known.items() if name in student.programme.lower()),
            None,
        )
        if match is not None:
            student.department = match
            student.college = match.college
            student.save(update_fields=['department', 'college'])


def unseed(apps, schema_editor):
    """Detach the students; leave the records.

    The programme text was never removed, so nothing a student had is lost by
    unlinking. Deleting the colleges would throw away anything added since.
    """
    Student = apps.get_model('scheduler', 'Student')
    Student.objects.update(department=None, college=None)


class Migration(migrations.Migration):

    dependencies = [
        ('scheduler', '0011_college_alter_student_programme_student_college_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
