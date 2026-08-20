"""Bulk loading reference data from CSV.

One gate: the file has to be the kind of file you said it was. That is decided
by its header row - a rooms file has name and capacity, a lecturers file has
name and email - so uploading rooms into Lecturers is refused, which is the
mistake worth catching.

Past that gate the importer is deliberately permissive. It loads every row it
can and reports the ones it could not, rather than rejecting a whole file over
a few bad lines. A row is only skipped when the record genuinely cannot exist
without the missing piece: a lecturer needs an email, a room needs a capacity.

Anything a row refers to that does not exist yet - a student's group, a
course's lecturer - is created rather than treated as an error, and the count
is reported so nothing appears out of nowhere unannounced.

Rows are matched on the identifier they already carry (email, course code,
room name, student ID) and update the existing record, so re-uploading a
corrected file fixes data instead of duplicating it.
"""
import csv
import io
import re
from datetime import datetime

from .models import (
    DAY_CHOICES, Course, Department, Lecturer, Room, Student, StudentGroup,
    TimeSlot,
)


def match_department(programme, departments):
    """The department a programme names, if this system has one.

    A roster writes "BSc Computer Science" where the department is called
    Computer Science, so the department name is looked for inside the
    programme rather than compared to it. Longest first, so "Computer
    Science" is preferred over a department called "Science" that happens to
    be a substring of it.
    """
    if not programme:
        return None
    text = programme.lower()
    for name, department in departments:
        if name in text:
            return department
    return None


def department_lookup():
    """Departments, longest name first, ready for match_department."""
    return sorted(
        ((d.name.lower(), d) for d in Department.objects.select_related('college')),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )

# Excel writes UTF-8 with a byte-order mark, which would otherwise turn the
# first header into "﻿name" and fail every lookup against it.
ENCODING = 'utf-8-sig'

# Delimiters a spreadsheet might have used. Semicolon is what Excel writes in
# locales where the comma is the decimal separator, and a file saved that way
# arrives as a single column with the entire header inside it.
DELIMITERS = [',', ';', '\t', '|']

# Real exports do not use our column names. Headers are compared with case,
# spaces, underscores and punctuation removed, so "Email Address", "e-mail"
# and "EMAIL" all arrive here as "emailaddress", "email" and "email".
ALIASES = {
    'name': [
        'name', 'fullname', 'lecturername', 'staffname', 'roomname', 'venue',
        'venuename', 'coursename', 'coursetitle', 'title', 'studentname',
        'surname', 'names',
    ],
    'email': [
        'email', 'emailaddress', 'mail', 'mailaddress', 'lectureremail',
        'staffemail', 'institutionalemail', 'schoolemail', 'contact',
        'contactemail',
    ],
    'capacity': [
        'capacity', 'seats', 'seat', 'size', 'seatingcapacity', 'roomcapacity',
        'numberofseats', 'noofseats', 'maxcapacity', 'maximumcapacity',
    ],
    'code': [
        'code', 'coursecode', 'courseid', 'subjectcode', 'modulecode',
        'catalognumber',
    ],
    'expected_students': [
        'expectedstudents', 'students', 'classsize', 'enrollment', 'enrolment',
        'expected', 'numberofstudents', 'noofstudents', 'studentcount',
        'expectedenrollment', 'expectedenrolment',
    ],
    'lecturer_email': [
        'lectureremail', 'lecturer', 'lecturermail', 'staffemail', 'teacher',
        'teacheremail', 'instructor', 'instructoremail', 'email',
        'emailaddress',
    ],
    'lecturer_id': [
        'lecturerid', 'staffid', 'staffnumber', 'staffno', 'employeeid',
        'employeeno', 'lecturernumber', 'lecturerno',
    ],
    # Index number is its own field, so it must not be treated as another
    # spelling of the student ID - the two numbers are different numbers.
    'student_id': [
        'studentid', 'id', 'studentnumber', 'studentno', 'referencenumber',
        'refno', 'reference', 'matricnumber', 'matricno',
    ],
    'index_number': [
        'indexnumber', 'indexno', 'index', 'examnumber', 'examno',
        'examinationnumber', 'examindex',
    ],
    'programme': [
        'programme', 'program', 'degree', 'major', 'course', 'courseofstudy',
        'department', 'discipline',
    ],
    'level': ['level', 'yearofstudy', 'stage', 'year', 'currentlevel'],
    'day': ['day', 'weekday', 'dayofweek', 'days'],
    'start_time': [
        'starttime', 'start', 'from', 'begins', 'begin', 'startsat', 'timefrom',
    ],
    'end_time': [
        'endtime', 'end', 'to', 'finishes', 'finish', 'until', 'endsat', 'timeto',
    ],
    'group': [
        'group', 'studentgroup', 'cohort', 'section', 'classgroup', 'class',
        'programmeandlevel', 'teachinggroup',
    ],
}


def normalise_header(header):
    """Strip everything that varies between one spreadsheet and the next."""
    return re.sub(r'[^a-z0-9]', '', (header or '').lower())


class ImportError_(Exception):
    """A problem with the file as a whole - wrong kind, unreadable, empty."""


class ImportResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.auto_created = []   # things invented to satisfy a reference
        self.skipped = []        # rows that could not become a record
        self.rows_read = 0
        self.repeated = 0        # rows whose identifier a previous row already used
        self.repeated_examples = []
        self.mapping = {}        # our field -> the header it was read from
        self.identifier_field = None

    @property
    def total(self):
        """Distinct records created or updated.

        Rows sharing an identifier are collapsed before anything is written -
        the last one wins - so this counts records, not rows. It is the number
        that ends up on the list page, and `repeated` explains any gap between
        it and `rows_read`.
        """
        return self.created + self.updated

    # Kept as a name that says what it means at the call site.
    records = total

    @property
    def imported_anything(self):
        return self.total > 0

    @property
    def identifier_column(self):
        """The header(s) the key was read from, for diagnosis.

        A time slot is keyed by three columns together, so this can name more
        than one.
        """
        headers = [
            self.mapping.get(f) for f in (self.identifier_field or ())
            if self.mapping.get(f)
        ]
        return ' + '.join(headers) if headers else None


def _clean(value):
    return (value or '').strip()


def _whole_number(value):
    """The number, or None if it is not one."""
    text = _clean(value)
    if not text:
        return None
    try:
        number = int(float(text))   # tolerate "250.0" from a spreadsheet
    except ValueError:
        return None
    return number if number > 0 else None


# --- one handler per kind ----------------------------------------------------

BATCH = 500


def _apply(model, to_create, to_update, fields, result):
    """Write a whole import in a handful of statements.

    One round trip per batch rather than one per row. Against a database on the
    far side of a network that is the difference between a few seconds and a
    request the gateway gives up on.
    """
    if to_create:
        model.objects.bulk_create(to_create, batch_size=BATCH)
    if to_update:
        model.objects.bulk_update(to_update, fields, batch_size=BATCH)
    result.created += len(to_create)
    result.updated += len(to_update)


def _import_lecturers(rows, result):
    wanted = {}   # lowered email -> (email, name); a later row replaces an earlier
    for line, row in rows:
        email = _clean(row.get('email')).lower()
        if not email:
            result.skipped.append(f'Row {line}: no email, so there is nothing to identify this lecturer by')
            continue
        wanted[email] = (
            email,
            _clean(row.get('name')) or email.split('@')[0],
            # Blank rather than absent is the common case - the column exists
            # in the file and not every row has one filled in.
            _clean(row.get('lecturer_id')),
        )

    existing = {
        l.email.lower(): l
        for l in Lecturer.objects.filter(
            email__in=[e for e, _name, _id in wanted.values()])
    }

    # A lecturer ID is unique, so one already held by somebody outside this
    # import is dropped from the row rather than failing it - the rest of the
    # record is still worth having, and the clash is reported.
    ids = {i for _e, _n, i in wanted.values() if i}
    owned_elsewhere = set(
        Lecturer.objects
        .filter(lecturer_id__in=ids)
        .exclude(email__in=[e for e, _n, _i in wanted.values()])
        .values_list('lecturer_id', flat=True)
    )

    to_create, to_update = [], []
    claimed = set()
    for key, (email, name, lecturer_id) in wanted.items():
        if lecturer_id and (lecturer_id in owned_elsewhere or lecturer_id in claimed):
            result.skipped.append(
                f'Lecturer ID "{lecturer_id}" already belongs to another '
                f'lecturer, so it was left off {email}'
            )
            lecturer_id = ''
        elif lecturer_id:
            claimed.add(lecturer_id)

        # bulk_create and bulk_update do not call save(), so the empty-string
        # to NULL conversion that keeps the unique constraint happy has to
        # happen here.
        id_value = lecturer_id or None

        found = existing.get(key)
        if found is None:
            to_create.append(Lecturer(email=email, name=name, lecturer_id=id_value))
        else:
            found.name = name
            # Only overwrite when the file supplies one. A blank column, or one
            # dropped for clashing, must not erase the ID already on file.
            if id_value:
                found.lecturer_id = id_value
            to_update.append(found)
    _apply(Lecturer, to_create, to_update, ['name', 'lecturer_id'], result)


def _import_rooms(rows, result):
    wanted = {}
    for line, row in rows:
        name = _clean(row.get('name'))
        if not name:
            result.skipped.append(f'Row {line}: no name, so there is nothing to identify this room by')
            continue
        capacity = _whole_number(row.get('capacity'))
        if capacity is None:
            result.skipped.append(
                f'Row {line}: "{name}" has no usable capacity '
                f'("{_clean(row.get("capacity"))}") - a room needs a number of seats'
            )
            continue
        wanted[name.lower()] = (name, capacity)

    existing = {
        r.name.lower(): r
        for r in Room.objects.filter(name__in=[n for n, _ in wanted.values()])
    }

    to_create, to_update = [], []
    for key, (name, capacity) in wanted.items():
        found = existing.get(key)
        if found is None:
            to_create.append(Room(name=name, capacity=capacity))
        else:
            found.capacity = capacity
            to_update.append(found)
    _apply(Room, to_create, to_update, ['capacity'], result)


def _import_courses(rows, result):
    wanted = {}
    for line, row in rows:
        code = _clean(row.get('code'))
        if not code:
            result.skipped.append(f'Row {line}: no course code, so there is nothing to identify this course by')
            continue
        wanted[code.lower()] = (
            code,
            _clean(row.get('name')) or code,
            _whole_number(row.get('expected_students')),
            _clean(row.get('lecturer_email')).lower(),
        )

    # Resolve every lecturer at once, creating the unknown ones in one go
    # rather than refusing the row.
    emails = {email for *_rest, email in wanted.values() if email}
    lecturers = {
        l.email.lower(): l for l in Lecturer.objects.filter(email__in=emails)
    }
    missing = [e for e in emails if e not in lecturers]
    if missing:
        # The name is a placeholder from the address, corrected by importing
        # the real lecturers file or editing the record.
        Lecturer.objects.bulk_create(
            [Lecturer(email=e, name=e.split('@')[0]) for e in missing],
            batch_size=BATCH,
        )
        for l in Lecturer.objects.filter(email__in=missing):
            lecturers[l.email.lower()] = l
        result.auto_created.extend(f'lecturer "{e}"' for e in missing)

    existing = {
        c.code.lower(): c
        for c in Course.objects.filter(code__in=[c for c, *_ in wanted.values()])
    }

    to_create, to_update = [], []
    for key, (code, name, expected, email) in wanted.items():
        lecturer = lecturers.get(email) if email else None
        found = existing.get(key)
        if found is None:
            course = Course(code=code, name=name, lecturer=lecturer)
            if expected is not None:
                course.expected_students = expected
            to_create.append(course)
        else:
            found.name = name
            found.lecturer = lecturer
            if expected is not None:
                found.expected_students = expected
            to_update.append(found)
    _apply(Course, to_create, to_update,
           ['name', 'lecturer', 'expected_students'], result)


# "MON", "Monday", "monday", "Mon." all mean the same day. Deliberately no
# single-letter forms: T and S are ambiguous, and guessing is worse than asking.
DAY_LOOKUP = {}
for _code, _label in DAY_CHOICES:
    DAY_LOOKUP[_code.lower()] = _code
    DAY_LOOKUP[_label.lower()] = _code
    DAY_LOOKUP[_label.lower()[:3]] = _code

TIME_FORMATS = ['%H:%M', '%H:%M:%S', '%I:%M%p', '%I:%M %p', '%I%p', '%I %p', '%H%M']


def _normalise_day(value):
    return DAY_LOOKUP.get(re.sub(r'[^a-z]', '', _clean(value).lower()), '')


def _parse_time(value):
    """Read a time as a spreadsheet is likely to have written it."""
    text = _clean(value).upper().replace('.', ':')
    text = re.sub(r'\s+', ' ', text)
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _normalise_level(value):
    """Accept "400", "Level 400", "L400", "4" and similar."""
    text = _clean(value)
    if not text:
        return ''
    digits = re.sub(r'[^0-9]', '', text)
    if not digits:
        return ''
    # A bare year - "4" - is the level in hundreds.
    if len(digits) == 1 and digits != '0':
        digits = f'{digits}00'
    valid = {choice for choice, _label in Student.LEVELS}
    return digits if digits in valid else ''


def _import_students(rows, result):
    wanted = {}
    for line, row in rows:
        student_id = _clean(row.get('student_id'))
        if not student_id:
            result.skipped.append(f'Row {line}: no student ID, which is what a student signs in with')
            continue
        wanted[student_id.lower()] = {
            'line': line,
            'student_id': student_id,
            'name': _clean(row.get('name')) or student_id,
            'email': _clean(row.get('email')).lower(),
            'programme': _clean(row.get('programme')),
            'level': _normalise_level(row.get('level')),
            'group_name': _clean(row.get('group')),
            'index_number': _clean(row.get('index_number')),
        }

    # Every group in one pass, creating the unknown ones together.
    names = {r['group_name'] for r in wanted.values() if r['group_name']}
    groups = {
        g.name.lower(): g for g in StudentGroup.objects.filter(name__in=names)
    }
    missing = [n for n in names if n.lower() not in groups]
    if missing:
        StudentGroup.objects.bulk_create(
            [StudentGroup(name=n) for n in missing], batch_size=BATCH
        )
        for g in StudentGroup.objects.filter(name__in=missing):
            groups[g.name.lower()] = g
        result.auto_created.extend(f'student group "{n}"' for n in missing)

    existing = {
        s.student_id.lower(): s
        for s in Student.objects.filter(
            student_id__in=[r['student_id'] for r in wanted.values()]
        )
    }

    # An index number is unique, so one already held by a student outside this
    # import is dropped from the row rather than failing it - the rest of the
    # record is still worth having, and the clash is reported.
    indexes = {r['index_number'] for r in wanted.values() if r['index_number']}
    owned_elsewhere = set(
        Student.objects
        .filter(index_number__in=indexes)
        .exclude(student_id__in=[r['student_id'] for r in wanted.values()])
        .values_list('index_number', flat=True)
    )

    # bulk_create and bulk_update do not call save(), so the rule that a
    # programme follows its department has to be applied here as well as there.
    departments = department_lookup()

    to_create, to_update = [], []
    claimed = set()
    for key, r in wanted.items():
        index_number = r['index_number']
        if index_number and (index_number in owned_elsewhere or index_number in claimed):
            result.skipped.append(
                f'Row {r["line"]}: index number "{index_number}" already belongs to '
                f'another student, so it was left off {r["student_id"]}'
            )
            index_number = ''
        elif index_number:
            claimed.add(index_number)

        # bulk_create and bulk_update do not call save(), so the empty-string
        # to NULL conversion that keeps the unique constraint happy has to
        # happen here.
        index_value = index_number or None
        group = groups.get(r['group_name'].lower()) if r['group_name'] else None

        # A programme the system knows as a department becomes that
        # department, and the text settles on the department's own name so the
        # two cannot disagree. A programme it does not recognise is kept as
        # written - the roster is still right, this system is just missing a
        # department for it.
        department = match_department(r['programme'], departments)
        programme = department.name if department else r['programme']
        college = department.college if department else None

        found = existing.get(key)
        if found is None:
            to_create.append(Student(
                student_id=r['student_id'], name=r['name'], email=r['email'],
                programme=programme, level=r['level'], department=department,
                college=college, group=group, index_number=index_value,
            ))
        else:
            found.name = r['name']
            found.email = r['email']
            found.programme = programme
            found.department = department
            found.college = college
            found.level = r['level']
            found.group = group
            # Only overwrite an index number when the file supplies a usable
            # one. A blank column, or one dropped for clashing, must not erase
            # the number already on file.
            if index_value:
                found.index_number = index_value
            to_update.append(found)

    _apply(Student, to_create, to_update,
           ['name', 'email', 'programme', 'department', 'college', 'level',
            'group', 'index_number'], result)


def _import_timeslots(rows, result):
    wanted = {}
    for line, row in rows:
        day = _normalise_day(row.get('day'))
        if not day:
            result.skipped.append(
                f'Row {line}: "{_clean(row.get("day"))}" is not a weekday. Use '
                f'Monday to Friday, or MON to FRI.'
            )
            continue

        start = _parse_time(row.get('start_time'))
        end = _parse_time(row.get('end_time'))
        if start is None or end is None:
            which = 'start' if start is None else 'end'
            result.skipped.append(
                f'Row {line}: cannot read the {which} time '
                f'("{_clean(row.get(which + "_time"))}"). Use 08:00 or 8:00 AM.'
            )
            continue
        if end <= start:
            result.skipped.append(
                f'Row {line}: {day} {start:%H:%M}-{end:%H:%M} ends before it starts.'
            )
            continue

        wanted[(day, start, end)] = (day, start, end)

    # A slot is identified by all three fields together, so existing rows are
    # matched on the triple rather than on any one column.
    existing = {
        (t.day, t.start_time, t.end_time)
        for t in TimeSlot.objects.filter(day__in={d for d, _s, _e in wanted})
    }
    to_create = [
        TimeSlot(day=day, start_time=start, end_time=end)
        for key, (day, start, end) in wanted.items()
        if key not in existing
    ]
    # Nothing to update: the three fields are the whole record, so a slot that
    # already exists is already correct.
    _apply(TimeSlot, to_create, [], [], result)
    result.updated += len(wanted) - len(to_create)


KINDS = {
    'lecturers': {
        'label': 'Lecturers',
        'columns': ['lecturer_id', 'name', 'email'],
        # What makes a file this kind of file. Uploading rooms into Lecturers
        # fails here, which is the mistake worth catching.
        'identifies': ['email'],
        # What records are matched on, which is not always what identifies the
        # file: a rooms file is recognised by its capacity column, but its
        # records are keyed by name.
        'key': ('email',),
        'handler': _import_lecturers,
        'sample': [
            ['KNUST/CS/014', 'Dr. Kwame Mensah', 'kmensah@knust.edu.gh'],
            ['KNUST/CS/021', 'Prof. Ama Boateng', 'aboateng@knust.edu.gh'],
        ],
        'note': 'Matched on email. Re-uploading updates the name and ID. A '
                'lecturer needs an ID before they can set up their own account.',
    },
    'rooms': {
        'label': 'Rooms',
        'columns': ['name', 'capacity'],
        'identifies': ['capacity'],
        'key': ('name',),
        'handler': _import_rooms,
        'sample': [
            ['PB 001 Lecture Hall', '250'],
            ['CS Lab 1', '60'],
        ],
        'note': 'Matched on name. Capacity is a number of seats.',
    },
    'courses': {
        'label': 'Courses',
        'columns': ['code', 'name', 'expected_students', 'lecturer_email'],
        'identifies': ['code'],
        'key': ('code',),
        'handler': _import_courses,
        'sample': [
            ['CS 151', 'Introduction to Programming', '220', 'kmensah@knust.edu.gh'],
            ['CS 153', 'Discrete Mathematics', '210', ''],
        ],
        'note': (
            'Matched on code. A lecturer_email that is not on file yet is '
            'created for you.'
        ),
    },
    'students': {
        'label': 'Students',
        'columns': ['student_id', 'index_number', 'name', 'email', 'programme',
                    'level', 'group'],
        'identifies': ['student_id'],
        'key': ('student_id',),
        'handler': _import_students,
        'sample': [
            ['20512001', '7212001', 'Ama Serwaa', 'aserwaa@st.knust.edu.gh',
             'BSc Computer Science', '100', 'CS Level 100'],
            ['20412003', '7212003', 'Abena Frimpong', 'afrimpong@st.knust.edu.gh',
             'BSc Computer Science', '200', 'CS Level 200'],
        ],
        'note': (
            'Matched on student_id, which is also what they sign in with. Only '
            'that column is required; a group that does not exist yet is created '
            'for you, and level accepts "400" or "Level 400". The email is only '
            'used for password resets, and a student without one cannot reset '
            'their own.'
        ),
    },
    'timeslots': {
        'label': 'Time Slots',
        'columns': ['day', 'start_time', 'end_time'],
        'identifies': ['day'],
        # All three fields together are the record, so all three are the key.
        'key': ('day', 'start_time', 'end_time'),
        'handler': _import_timeslots,
        'sample': [
            ['Monday', '08:00', '10:00'],
            ['Monday', '10:00', '12:00'],
            ['Tuesday', '08:00', '10:00'],
        ],
        'note': (
            'A slot is the day plus its start and end, so re-importing the same '
            'file adds nothing. Days may be written Monday or MON; times 08:00 '
            'or 8:00 AM.'
        ),
    },
}


def template_csv(kind):
    spec = KINDS[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(spec['columns'])
    writer.writerows(spec['sample'])
    return buffer.getvalue()


def _sniff_delimiter(first_line):
    """Whichever separator carves the header into the most columns."""
    best, best_count = ',', 1
    for candidate in DELIMITERS:
        count = len(next(csv.reader([first_line], delimiter=candidate)))
        if count > best_count:
            best, best_count = candidate, count
    return best


def map_headers(fieldnames, kind):
    """Match this file's headers to our field names.

    Returns {our field: their header}. A header is claimed by at most one
    field, and earlier fields in the spec win, so a courses file with a single
    "email" column gives it to lecturer_email rather than leaving it unused.
    """
    spec = KINDS[kind]
    available = {}
    for header in fieldnames:
        key = normalise_header(header)
        if key:
            available.setdefault(key, header)

    mapping = {}
    claimed = set()
    for field in spec['columns']:
        for alias in ALIASES.get(field, [field]):
            if alias in available and available[alias] not in claimed:
                mapping[field] = available[alias]
                claimed.add(available[alias])
                break
    return mapping


def _match_score(fieldnames, kind):
    """How completely this file matches a kind.

    Zero unless the identifying columns are all there, so a file can never be
    claimed by a kind it could not populate. Beyond that, more matched columns
    is a better match, and matching an identifying column counts double - a
    course code is far stronger evidence of a courses file than a name column
    is of anything.
    """
    spec = KINDS[kind]
    mapping = map_headers(fieldnames, kind)
    if any(c not in mapping for c in spec['identifies']):
        return 0
    return sum(2 if field in spec['identifies'] else 1 for field in mapping)


def _elsewhere_hint(fieldnames, kind):
    others = [
        other for other in KINDS
        if other != kind and _match_score(fieldnames, other) > 0
    ]
    if not others:
        return ''
    names = ' or '.join(KINDS[o]['label'] for o in others)
    return f' It looks more like a {names} file - try importing it there.'


def read_rows(upload, kind):
    """Decode, check the file is the kind claimed, and return its rows."""
    spec = KINDS[kind]
    raw = upload.read()
    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError:
        raise ImportError_(
            'That file is not readable as text. If it came from Excel, save it '
            'as "CSV UTF-8" and try again.'
        )
    if not text.strip():
        raise ImportError_('That file is empty.')

    delimiter = _sniff_delimiter(text.splitlines()[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise ImportError_('That file is empty.')

    mapping = map_headers(reader.fieldnames, kind)
    found = ', '.join(f'"{h.strip()}"' for h in reader.fieldnames if (h or '').strip())

    # The one gate: does this look like the kind of file it was uploaded as?
    missing = [c for c in spec['identifies'] if c not in mapping]
    if missing:
        hint = _elsewhere_hint(reader.fieldnames, kind)
        raise ImportError_(
            f'This does not look like a {spec["label"]} file: nothing in it looks '
            f'like a {" or ".join(missing)} column. '
            f'Its columns are: {found or "(none)"}. '
            f'A {spec["label"]} file needs a column for '
            f'{", ".join(spec["identifies"])}.{hint}'
        )

    # Identity alone is not enough. A courses file carries lecturer_email, which
    # would pass as a Lecturers file and then import course names as people. So
    # the file goes to whichever kind it matches most completely.
    best, best_score = kind, _match_score(reader.fieldnames, kind)
    for other in KINDS:
        if other == kind:
            continue
        score = _match_score(reader.fieldnames, other)
        if score > best_score:
            best, best_score = other, score
    if best != kind:
        raise ImportError_(
            f'This looks like a {KINDS[best]["label"]} file rather than a '
            f'{spec["label"]} file - its columns are: {found}. '
            f'Import it under {KINDS[best]["label"]} instead. If it really is '
            f'{spec["label"].lower()}, remove the columns that belong to '
            f'{KINDS[best]["label"].lower()} and upload it again.'
        )

    rows = []
    for index, row in enumerate(reader, start=2):  # row 1 is the header
        if not any(_clean(v) for v in row.values()):
            continue  # a blank line, which spreadsheets leave behind
        # Re-key onto our field names, so handlers never see their spelling.
        rows.append((index, {field: row.get(header) for field, header in mapping.items()}))

    if not rows:
        raise ImportError_('That file has a header row but no data in it.')
    return rows, mapping


def _count_repeats(rows, spec, result):
    """How many rows reuse an identifier an earlier row already claimed.

    Records are matched on that identifier, so a repeat overwrites rather than
    adds. Counting the collapse here is the difference between "623 updated",
    which sounds like success, and "623 rows overwrote each other", which is
    what actually happened.
    """
    # The key records are matched on, not the column that identifies the file
    # type. For rooms those differ, and using the wrong one counts no repeats.
    # A time slot is keyed by all three of its fields together.
    fields = spec['key']
    result.identifier_field = fields
    seen = set()
    repeated = []
    for _line, row in rows:
        parts = [_clean(row.get(f)) for f in fields]
        if not all(parts):
            continue
        key = tuple(p.lower() for p in parts)
        if key in seen:
            repeated.append(' '.join(parts))
        seen.add(key)
    result.repeated = len(repeated)
    # A few is enough to recognise the pattern; the whole list is noise.
    result.repeated_examples = list(dict.fromkeys(repeated))[:5]


def run_import(kind, upload):
    """Load everything loadable, and report whatever could not be."""
    spec = KINDS[kind]
    result = ImportResult()
    rows, mapping = read_rows(upload, kind)
    result.rows_read = len(rows)
    result.mapping = mapping
    _count_repeats(rows, spec, result)
    spec['handler'](rows, result)

    # Every row unusable, despite the headers being right, means the columns
    # are named correctly but hold something else entirely.
    if not result.imported_anything and result.skipped:
        raise ImportError_(
            f'None of the {len(rows)} rows could be read as {spec["label"].lower()}. '
            f'The columns are named correctly but do not contain '
            f'{spec["label"].lower()} data. First problem: {result.skipped[0]}'
        )
    return result
