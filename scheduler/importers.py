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

from .models import Course, Lecturer, Room, Student, StudentGroup

# Excel writes UTF-8 with a byte-order mark, which would otherwise turn the
# first header into "﻿name" and fail every lookup against it.
ENCODING = 'utf-8-sig'


class ImportError_(Exception):
    """A problem with the file as a whole - wrong kind, unreadable, empty."""


class ImportResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.auto_created = []   # things invented to satisfy a reference
        self.skipped = []        # rows that could not become a record

    @property
    def total(self):
        return self.created + self.updated

    @property
    def imported_anything(self):
        return self.total > 0


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

def _import_lecturers(rows, result):
    for line, row in rows:
        email = _clean(row.get('email')).lower()
        name = _clean(row.get('name'))
        if not email:
            result.skipped.append(f'Row {line}: no email, so there is nothing to identify this lecturer by')
            continue
        _, created = Lecturer.objects.update_or_create(
            email=email,
            defaults={'name': name or email.split('@')[0]},
        )
        result.created += created
        result.updated += not created


def _import_rooms(rows, result):
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
        _, created = Room.objects.update_or_create(
            name=name, defaults={'capacity': capacity}
        )
        result.created += created
        result.updated += not created


def _import_courses(rows, result):
    lecturers = {l.email.lower(): l for l in Lecturer.objects.all()}
    for line, row in rows:
        code = _clean(row.get('code'))
        if not code:
            result.skipped.append(f'Row {line}: no course code, so there is nothing to identify this course by')
            continue

        lecturer = None
        email = _clean(row.get('lecturer_email')).lower()
        if email:
            lecturer = lecturers.get(email)
            if lecturer is None:
                # Create rather than refuse. The name is a placeholder taken
                # from the address, and is corrected by importing the real
                # lecturers file or editing the record.
                lecturer = Lecturer.objects.create(
                    email=email, name=email.split('@')[0]
                )
                lecturers[email] = lecturer
                result.auto_created.append(f'lecturer "{email}"')

        expected = _whole_number(row.get('expected_students'))
        defaults = {
            'name': _clean(row.get('name')) or code,
            'lecturer': lecturer,
        }
        if expected is not None:
            defaults['expected_students'] = expected

        _, created = Course.objects.update_or_create(code=code, defaults=defaults)
        result.created += created
        result.updated += not created


def _import_students(rows, result):
    groups = {g.name.lower(): g for g in StudentGroup.objects.all()}
    for line, row in rows:
        student_id = _clean(row.get('student_id'))
        if not student_id:
            result.skipped.append(f'Row {line}: no student ID, which is what a student signs in with')
            continue

        group = None
        group_name = _clean(row.get('group'))
        if group_name:
            group = groups.get(group_name.lower())
            if group is None:
                group = StudentGroup.objects.create(name=group_name)
                groups[group_name.lower()] = group
                result.auto_created.append(f'student group "{group_name}"')

        _, created = Student.objects.update_or_create(
            student_id=student_id,
            defaults={'name': _clean(row.get('name')) or student_id, 'group': group},
        )
        result.created += created
        result.updated += not created


KINDS = {
    'lecturers': {
        'label': 'Lecturers',
        'columns': ['name', 'email'],
        # What makes a file this kind of file. Uploading rooms into Lecturers
        # fails here, which is the mistake worth catching.
        'identifies': ['email'],
        'handler': _import_lecturers,
        'sample': [
            ['Dr. Kwame Mensah', 'kmensah@knust.edu.gh'],
            ['Prof. Ama Boateng', 'aboateng@knust.edu.gh'],
        ],
        'note': 'Matched on email. Re-uploading updates the name.',
    },
    'rooms': {
        'label': 'Rooms',
        'columns': ['name', 'capacity'],
        'identifies': ['capacity'],
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
        'columns': ['student_id', 'name', 'group'],
        'identifies': ['student_id'],
        'handler': _import_students,
        'sample': [
            ['20512001', 'Ama Serwaa', 'CS Level 100'],
            ['20412003', 'Abena Frimpong', 'CS Level 200'],
        ],
        'note': (
            'Matched on student_id, which is also what they sign in with. A '
            'group that does not exist yet is created for you.'
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


def _identifying_column_report(kind):
    """Which kind of file the headers actually look like, for a better error."""
    return {k: spec['identifies'] for k, spec in KINDS.items() if k != kind}


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

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_('That file is empty.')

    headers = {(h or '').strip().lower() for h in reader.fieldnames}

    # The one real gate: does this look like the kind of file it was uploaded as?
    missing = [c for c in spec['identifies'] if c not in headers]
    if missing:
        looks_like = [
            other for other, cols in _identifying_column_report(kind).items()
            if all(c in headers for c in cols)
        ]
        hint = ''
        if looks_like:
            names = ' or '.join(KINDS[o]['label'] for o in looks_like)
            hint = f' It looks like a {names} file - try importing it there instead.'
        raise ImportError_(
            f'This does not look like a {spec["label"]} file: it has no '
            f'{", ".join(missing)} column. A {spec["label"]} file needs a header '
            f'row containing {", ".join(spec["columns"])}.{hint}'
        )

    rows = []
    for index, row in enumerate(reader, start=2):  # row 1 is the header
        normalised = {(k or '').strip().lower(): v for k, v in row.items()}
        if not any(_clean(v) for v in normalised.values()):
            continue  # a blank line, which spreadsheets leave behind
        rows.append((index, normalised))

    if not rows:
        raise ImportError_('That file has a header row but no data in it.')
    return rows


def run_import(kind, upload):
    """Load everything loadable, and report whatever could not be."""
    spec = KINDS[kind]
    result = ImportResult()
    rows = read_rows(upload, kind)
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
