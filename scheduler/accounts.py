"""Creating login accounts for lecturers.

Shared by the management command and the admin-facing button, because a host
without shell access needs the second and a bulk import needs the first.
"""
import secrets
import string

# Ambiguous characters removed: these passwords get read aloud and typed by hand.
ALPHABET = ''.join(c for c in string.ascii_letters + string.digits if c not in 'Il1O0')


def generate_password(length=14):
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))


def derive_username(email, taken):
    """A username from the email's local part, suffixed until it is free."""
    base = email.split('@')[0].lower().replace('.', '') or 'lecturer'
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f'{base}{suffix}'
        suffix += 1
    return candidate


def create_student_account(student):
    """Give a student a login. The username is their student ID.

    Deliberately not derived or prefixed: the requirement is that a student
    signs in with their assigned credentials, and the ID is the one identifier
    they already have. Returns (user, password); the password is shown once and
    never stored in readable form.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    password = generate_password()
    # The address is copied onto the account because that is where Django's
    # password reset looks; a student without one simply cannot self-serve.
    user = User.objects.create_user(
        username=student.student_id, password=password, email=student.email or '',
    )
    student.user = user
    student.save(update_fields=['user'])
    return user, password


def lecturer_username(lecturer, taken):
    """The username a lecturer's account should get.

    Their lecturer ID, so they sign in with the number they already have -
    the same bargain a student gets. Falling back to a name derived from their
    email covers the lecturers who predate the ID column, and the collision
    check covers the rest: a lecturer ID and a student ID come from different
    sequences with nothing stopping them meeting.

    Separate from creating the account because the bulk command offers a
    dry run, and a dry run has to be able to say what the username would be
    without making one.
    """
    if lecturer.lecturer_id and lecturer.lecturer_id not in taken:
        return lecturer.lecturer_id
    return derive_username(lecturer.email, taken)


def create_lecturer_account(lecturer):
    """Give a lecturer a login. The username is their lecturer ID.

    The same bargain as a student's: sign in with the number you already have.
    Where a lecturer has no ID on file - the ones who predate the column - the
    username is derived from their email instead, which is what the
    administrator's own create-account button has always done.

    Returns (user, password); the password is shown or sent once and never
    stored in readable form.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()
    taken = set(User.objects.values_list('username', flat=True))
    username = lecturer_username(lecturer, taken)
    password = generate_password()
    user = User.objects.create_user(
        username=username, password=password, email=lecturer.email or '',
    )
    lecturer.user = user
    lecturer.save(update_fields=['user'])
    return user, password
