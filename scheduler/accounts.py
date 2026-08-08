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
    user = User.objects.create_user(
        username=student.student_id, password=password
    )
    student.user = user
    student.save(update_fields=['user'])
    return user, password
