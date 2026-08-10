"""Bulk loading reference data from CSV.

Three decisions shape this, and they are worth stating because each rules out
something that looks simpler:

Validate everything, then commit, or commit nothing. A file that imports its
first forty rows and then stops on a typo leaves the database in a state
nobody asked for and the operator unable to tell what landed. Every row is
checked first; if any row fails, the whole file is rejected and every problem
is reported at once, so one round trip fixes all of them.

Rows are matched on the identifier the row already carries - a lecturer's
email, a course code, a room name, a student ID - and update the existing
record rather than adding a second one. Re-uploading a corrected file is
therefore safe and expected, rather than a way to double your data.

Referenced records must already exist. A course naming an unknown lecturer, or
a student naming an unknown group, is an error rather than a prompt to invent
one: a typo would otherwise silently create "CS Level 40" alongside "CS Level
400" and split a cohort in two.
"""
import csv
import io

from django.db import transaction

from .models import Course, Lecturer, Room, Student, StudentGroup

# Excel writes UTF-8 with a byte-order mark, which would otherwise turn the
# first header into "﻿name" and fail every lookup against it.
ENCODING = 'utf-8-sig'

MAX_ROWS = 5000


class ImportError_(Exception):
    """A problem with the file as a whole, rather than with a row."""


class ImportResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.errors = []

    @property
    def ok(self):
        return not self.errors

    @property
    def total(self):
        return self.created + self.updated


def _clean(value):
    return (value or '').strip()


def _positive_int(value, field):
    text = _clean(value)
    if not text:
        raise ValueError(f'{field} is required')
    try:
        number = int(text)
    except ValueError:
        raise ValueError(f'{field} must be a whole number, got "{text}"')
    if number < 1:
        raise ValueError(f'{field} must be at least 1, got {number}')
    return number


# --- one spec per kind -------------------------------------------------------

def _import_lecturers(rows, result):
    seen = set()
    prepared = []
    for line, row in rows:
        email = _clean(row.get('email')).lower()
        name = _clean(row.get('name'))
        if not name:
            result.errors.append(f'Row {line}: name is required')
            continue
        if not email:
            result.errors.append(f'Row {line}: email is required')
            continue
        if '@' not in email:
            result.errors.append(f'Row {line}: "{email}" is not an email address')
            continue
        if email in seen:
            result.errors.append(f'Row {line}: "{email}" appears twice in this file')
            continue
        seen.add(email)
        prepared.append((email, name))

    if not result.ok:
        return
    for email, name in prepared:
        _, created = Lecturer.objects.update_or_create(
            email=email, defaults={'name': name}
        )
        result.created += created
        result.updated += not created


def _import_rooms(rows, result):
    seen = set()
    prepared = []
    for line, row in rows:
        name = _clean(row.get('name'))
        if not name:
            result.errors.append(f'Row {line}: name is required')
            continue
        if name.lower() in seen:
            result.errors.append(f'Row {line}: "{name}" appears twice in this file')
            continue
        try:
            capacity = _positive_int(row.get('capacity'), 'capacity')
        except ValueError as exc:
            result.errors.append(f'Row {line}: {exc}')
            continue
        seen.add(name.lower())
        prepared.append((name, capacity))

    if not result.ok:
        return
    for name, capacity in prepared:
        _, created = Room.objects.update_or_create(
            name=name, defaults={'capacity': capacity}
        )
        result.created += created
        result.updated += not created


def _import_courses(rows, result):
    lecturers = {l.email.lower(): l for l in Lecturer.objects.all()}
    seen = set()
    prepared = []
    for line, row in rows:
        code = _clean(row.get('code'))
        name = _clean(row.get('name'))
        if not code:
            result.errors.append(f'Row {line}: code is required')
            continue
        if not name:
            result.errors.append(f'Row {line}: name is required')
            continue
        if code.lower() in seen:
            result.errors.append(f'Row {line}: "{code}" appears twice in this file')
            continue
        try:
            expected = _positive_int(row.get('expected_students'), 'expected_students')
        except ValueError as exc:
            result.errors.append(f'Row {line}: {exc}')
            continue

        lecturer = None
        email = _clean(row.get('lecturer_email')).lower()
        if email:
            lecturer = lecturers.get(email)
            if lecturer is None:
                result.errors.append(
                    f'Row {line}: no lecturer with email "{email}". Import '
                    f'lecturers first, or leave the column blank.'
                )
                continue

        seen.add(code.lower())
        prepared.append((code, name, expected, lecturer))

    if not result.ok:
        return
    for code, name, expected, lecturer in prepared:
        _, created = Course.objects.update_or_create(
            code=code,
            defaults={'name': name, 'expected_students': expected, 'lecturer': lecturer},
        )
        result.created += created
        result.updated += not created


def _import_students(rows, result):
    groups = {g.name.lower(): g for g in StudentGroup.objects.all()}
    seen = set()
    prepared = []
    for line, row in rows:
        student_id = _clean(row.get('student_id'))
        name = _clean(row.get('name'))
        if not student_id:
            result.errors.append(f'Row {line}: student_id is required')
            continue
        if not name:
            result.errors.append(f'Row {line}: name is required')
            continue
        if student_id.lower() in seen:
            result.errors.append(f'Row {line}: "{student_id}" appears twice in this file')
            continue

        group = None
        group_name = _clean(row.get('group'))
        if group_name:
            group = groups.get(group_name.lower())
            if group is None:
                known = ', '.join(sorted(g.name for g in groups.values())) or 'none yet'
                result.errors.append(
                    f'Row {line}: no student group called "{group_name}". '
                    f'Existing groups: {known}.'
                )
                continue

        seen.add(student_id.lower())
        prepared.append((student_id, name, group))

    if not result.ok:
        return
    for student_id, name, group in prepared:
        _, created = Student.objects.update_or_create(
            student_id=student_id, defaults={'name': name, 'group': group},
        )
        result.created += created
        result.updated += not created


KINDS = {
    'lecturers': {
        'label': 'Lecturers',
        'columns': ['name', 'email'],
        'required': ['name', 'email'],
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
        'required': ['name', 'capacity'],
        'handler': _import_rooms,
        'sample': [
            ['PB 001 Lecture Hall', '250'],
            ['CS Lab 1', '60'],
        ],
        'note': 'Matched on name. Capacity must be a whole number of seats.',
    },
    'courses': {
        'label': 'Courses',
        'columns': ['code', 'name', 'expected_students', 'lecturer_email'],
        'required': ['code', 'name', 'expected_students'],
        'handler': _import_courses,
        'sample': [
            ['CS 151', 'Introduction to Programming', '220', 'kmensah@knust.edu.gh'],
            ['CS 153', 'Discrete Mathematics', '210', ''],
        ],
        'note': 'Matched on code. Import lecturers first; lecturer_email may be blank.',
    },
    'students': {
        'label': 'Students',
        'columns': ['student_id', 'name', 'group'],
        'required': ['student_id', 'name'],
        'handler': _import_students,
        'sample': [
            ['20512001', 'Ama Serwaa', 'CS Level 100'],
            ['20412003', 'Abena Frimpong', 'CS Level 200'],
        ],
        'note': (
            'Matched on student_id, which is also what they sign in with. The '
            'group must already exist; leave it blank to assign later.'
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


def read_rows(upload, spec):
    """Decode and parse, raising ImportError_ for anything file-wide."""
    raw = upload.read()
    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError:
        raise ImportError_(
            'That file is not valid UTF-8 text. If it came from Excel, use '
            '"CSV UTF-8" when saving.'
        )

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_('The file is empty.')

    # Header comparison is case- and space-insensitive, because a spreadsheet
    # round trip is entitled to capitalise things.
    headers = {(h or '').strip().lower() for h in reader.fieldnames}
    missing = [c for c in spec['required'] if c not in headers]
    if missing:
        raise ImportError_(
            f'Missing column(s): {", ".join(missing)}. Expected a header row of: '
            f'{", ".join(spec["columns"])}.'
        )

    rows = []
    for index, row in enumerate(reader, start=2):  # row 1 is the header
        normalised = {(k or '').strip().lower(): v for k, v in row.items()}
        if not any(_clean(v) for v in normalised.values()):
            continue  # a blank line, which spreadsheets leave behind
        rows.append((index, normalised))
        if len(rows) > MAX_ROWS:
            raise ImportError_(
                f'That file has more than {MAX_ROWS} rows. Split it and import '
                f'in batches.'
            )

    if not rows:
        raise ImportError_('The file has a header but no data rows.')
    return rows


def run_import(kind, upload):
    """Validate every row, then commit them all or none of them."""
    spec = KINDS[kind]
    result = ImportResult()
    rows = read_rows(upload, spec)

    try:
        with transaction.atomic():
            spec['handler'](rows, result)
            if not result.ok:
                # Nothing was written yet - the handler returns before writing
                # when it found problems - but roll back regardless so a future
                # handler cannot quietly break the all-or-nothing promise.
                raise _Abort()
    except _Abort:
        pass
    return result


class _Abort(Exception):
    pass
