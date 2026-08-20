"""Signing in, and students and lecturers claiming records already held for them.

Three doors rather than one. They are not a security measure - anyone can find
the other two - and nothing here relies on people not knowing about them. What
they buy is that each audience gets a page written for it: a student is asked
for a student ID, a lecturer is not asked for one at all, and neither is shown
the timetable office's door. The role check on the way through is what makes
the doors mean anything: signing in at the wrong one sends you to the right
one rather than through.
"""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy

from .accounts import create_lecturer_account, create_student_account
from .forms import LecturerActivationForm, StudentActivationForm
from .permissions import is_admin, lecturer_for, student_for

logger = logging.getLogger(__name__)


# Which door suits which account, and where to send someone who used another.
DOORS = {
    'student': {
        'name': 'student',
        'url_name': 'student_login',
        'belongs': lambda user: student_for(user) is not None,
    },
    'lecturer': {
        'name': 'lecturer',
        'url_name': 'lecturer_login',
        'belongs': lambda user: lecturer_for(user) is not None,
    },
    'admin': {
        'name': 'timetable office',
        'url_name': 'admin_login',
        'belongs': is_admin,
    },
}


def door_for(user):
    """The door this account should have come through."""
    for key in ('admin', 'lecturer', 'student'):
        if DOORS[key]['belongs'](user):
            return key
    return None


class RoleLoginView(auth_views.LoginView):
    """A sign-in page for one kind of account.

    An account that does not belong here is refused and pointed at its own
    door. Without that the three pages would be decoration - the same login
    three times over - and a student signing in at the timetable office's door
    would land on a dashboard built for somebody else.
    """

    template_name = 'scheduler/login.html'
    door = 'student'

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {'door': self.door}

    def form_valid(self, form):
        user = form.get_user()
        if not DOORS[self.door]['belongs'](user):
            theirs = door_for(user)
            if theirs is None:
                # Signed in, and nothing to sign in to. An account with no
                # student, no lecturer and no admin rights has no dashboard.
                form.add_error(None,
                               'That account has no role yet. Ask the timetable '
                               'office to finish setting it up.')
                return self.form_invalid(form)

            logger.info('%s used the %s door, belongs at %s',
                        user.username, self.door, theirs)
            messages.info(
                self.request,
                f'That is a {DOORS[theirs]["name"]} account, so we have brought '
                f'you to the right sign-in page. Please try again here.',
            )
            return redirect(DOORS[theirs]['url_name'])
        return super().form_valid(form)


def login_chooser(request):
    """Who are you? - for anyone who arrives without knowing which door is theirs.

    This is where login_required sends people and where every {% url 'login' %}
    still points, so it has to keep working for someone who was part-way
    through something: the next page they were headed for is carried on to
    whichever door they pick.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')
    return render(request, 'scheduler/login_choose.html', {
        'next': request.GET.get('next', ''),
    })


class StudentLoginView(RoleLoginView):
    door = 'student'


class LecturerLoginView(RoleLoginView):
    door = 'lecturer'


class AdminLoginView(RoleLoginView):
    door = 'admin'


def activate_student(request):
    """First use: a student proves who they are and gets an account.

    Nothing is created here that was not already on the roster. The form finds
    the student record; this fills in what they told us about themselves and
    attaches a login to it.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = StudentActivationForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        student = form.cleaned_data['student']
        student.college = form.cleaned_data['college']
        student.department = form.cleaned_data['department']
        student.email = form.cleaned_data['email']
        # programme follows the department, in Student.save().
        student.save()

        user, password = create_student_account(student)
        logger.info('%s activated their own account', student.student_id)

        sent = _send_password(
            student.name, 'Student ID', student.student_id,
            student.email, password,
            'somebody has used your student ID and index number.',
        )
        if not sent:
            # The account exists either way; only the delivery failed, and
            # sending them back to the form would tell them to make a second.
            logger.error('Could not email the new password for %s',
                         student.student_id)

        return render(request, 'scheduler/activation_sent.html', {
            'name': student.name,
            'email': student.email,
            'identifier_label': 'student ID',
            'identifier': student.student_id,
            'sign_in_url': reverse('student_login'),
            'delivery_failed': not sent,
        })

    return render(request, 'scheduler/student_activate.html', {'form': form})


def _send_password(name, identifier_label, identifier, email, password, warning):
    """Send a newly created password. Returns whether it went.

    Shared by both activations, which differ only in what the person is called
    and what they should be told if it was not them who asked.
    """
    message = "\n".join([
        f"Hello {name},",
        "",
        "Your account is ready.",
        "",
        f"  {identifier_label}: {identifier}",
        f"  Password: {password}",
        "",
        "Sign in and change your password from My Account once you are in.",
        "",
        f"If you did not ask for this, tell the timetable office - {warning}",
        "",
    ])

    try:
        send_mail(
            subject="Your KNUST Timetable System account",
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        return True
    except Exception:
        # Everything, for the same reason the test-send catches everything: a
        # provider can refuse before a connection is even made, and the account
        # has already been created by this point.
        logger.exception("Activation email to %s failed", identifier)
        return False


def activate_lecturer(request):
    """First use: a lecturer proves who they are and gets an account.

    The mirror of the student flow, with one difference that matters. A
    student types in the address their password should go to; a lecturer's is
    already on file and is what they are checked against, so the password goes
    where the timetable office recorded it rather than where the form said.
    Guessing a lecturer ID therefore gains nothing - the mail arrives in the
    real lecturer's inbox.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = LecturerActivationForm(request.POST or None)

    if request.method == 'POST' and form.is_valid():
        lecturer = form.cleaned_data['lecturer']
        user, password = create_lecturer_account(lecturer)
        logger.info('%s activated their own account', lecturer.lecturer_id)

        sent = _send_password(
            lecturer.name, 'Lecturer ID', lecturer.lecturer_id,
            lecturer.email, password,
            'somebody has used your lecturer ID.',
        )
        if not sent:
            logger.error('Could not email the new password for %s',
                         lecturer.lecturer_id)

        return render(request, 'scheduler/activation_sent.html', {
            'name': lecturer.name,
            'email': lecturer.email,
            'identifier_label': 'lecturer ID',
            'identifier': lecturer.lecturer_id,
            'sign_in_url': reverse('lecturer_login'),
            'delivery_failed': not sent,
        })

    return render(request, 'scheduler/lecturer_activate.html', {'form': form})
