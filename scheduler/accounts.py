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
